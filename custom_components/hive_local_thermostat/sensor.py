"""Sensor platform for Hive Local Thermostat."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import (
    PRECISION_TENTHS,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.temperature import display_temp as show_temp

from .common import HiveConfigEntry
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
from .coordinator import HiveCoordinator
from .entity import HiveEntity, HiveEntityDescription


@dataclass(frozen=True, kw_only=True)
class HiveSensorEntityDescription(
    HiveEntityDescription,
    SensorEntityDescription,
):
    """Class describing Hive sensor entities."""

    icons_by_state: dict[str, str] | None = None


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: HiveConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""

    coordinator = config_entry.runtime_data.coordinator

    if coordinator.model == MODEL_SLR2:
        entity_descriptions = [
            HiveSensorEntityDescription(
                key="running_state_heat",
                translation_key="running_state_heat",
                name=config_entry.title,
            ),
            HiveSensorEntityDescription(
                key="local_temperature_heat",
                translation_key="local_temperature_heat",
                name=config_entry.title,
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                suggested_display_precision=1,
            ),
            HiveSensorEntityDescription(
                key="running_state_water",
                translation_key="running_state_water",
                name=config_entry.title,
            ),
            HiveSensorEntityDescription(
                key="boost_remaining_heat",
                translation_key="boost_remaining_heat",
                name=config_entry.title,
            ),
            HiveSensorEntityDescription(
                key="boost_remaining_water",
                translation_key="boost_remaining_water",
                name=config_entry.title,
            ),
        ]
    else:
        entity_descriptions = [
            HiveSensorEntityDescription(
                key="running_state_heat",
                translation_key="running_state_heat",
                name=config_entry.title,
            ),
            HiveSensorEntityDescription(
                key="local_temperature_heat",
                translation_key="local_temperature_heat",
                name=config_entry.title,
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                suggested_display_precision=1,
            ),
            HiveSensorEntityDescription(
                key="boost_remaining_heat",
                translation_key="boost_remaining_heat",
                name=config_entry.title,
                suggested_display_precision=1,
            ),
        ]

    _entities: list[HiveSensor | HiveWeeklyScheduleSensor] = [
        HiveSensor(
            entity_description=entity_description,
            coordinator=coordinator,
        )
        for entity_description in entity_descriptions
    ]

    _entities.append(
        HiveWeeklyScheduleSensor(
            entity_description=HiveSensorEntityDescription(
                key="weekly_schedule_heat",
                translation_key="weekly_schedule_heat",
                name=config_entry.title,
            ),
            coordinator=coordinator,
            zone=ZONE_HEAT,
        )
    )
    if coordinator.model == MODEL_SLR2:
        _entities.append(
            HiveWeeklyScheduleSensor(
                entity_description=HiveSensorEntityDescription(
                    key="weekly_schedule_water",
                    translation_key="weekly_schedule_water",
                    name=config_entry.title,
                ),
                coordinator=coordinator,
                zone=ZONE_WATER,
            )
        )

    async_add_entities(sensorEntity for sensorEntity in _entities)


class HiveSensor(HiveEntity, SensorEntity):
    """hive_local_thermostat Sensor class."""

    entity_description: HiveSensorEntityDescription

    def __init__(
        self,
        entity_description: HiveSensorEntityDescription,
        coordinator: HiveCoordinator,
    ) -> None:
        """Initialize the sensor class."""

        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{DOMAIN}_{entity_description.name}_{entity_description.key}".lower()
        )
        self._attr_has_entity_name = True

        super().__init__(entity_description, coordinator)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        try:
            new_value = getattr(self.coordinator, self.entity_description.key)
        except AttributeError:
            if self.entity_description.device_class == SensorDeviceClass.TEMPERATURE:
                new_value = 0
            else:
                new_value = ""

        if self.entity_description.device_class == SensorDeviceClass.TEMPERATURE:
            new_value = show_temp(
                self.hass,
                cast(float, new_value),
                self.entity_description.native_unit_of_measurement
                or UnitOfTemperature.CELSIUS,
                PRECISION_TENTHS,
            )

        self._attr_native_value = new_value
        self.async_write_ha_state()


class HiveWeeklyScheduleSensor(HiveEntity, SensorEntity):
    """Diagnostic sensor exposing the device's native weekly schedule.

    State is a simple "known days" summary; the full per-day transitions
    (as last read via the get_weekly_schedule service) are in the
    'schedule' extra attribute, since the raw data isn't a good fit for a
    sensor's native_value. Nothing is populated until get_weekly_schedule
    has been called at least once - this doesn't poll the device itself.
    """

    entity_description: HiveSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entity_description: HiveSensorEntityDescription,
        coordinator: HiveCoordinator,
        zone: str,
    ) -> None:
        """Initialize the sensor class."""

        self.entity_description = entity_description
        self._zone = zone
        self._attr_unique_id = (
            f"{DOMAIN}_{entity_description.name}_{entity_description.key}".lower()
        )
        self._attr_has_entity_name = True

        super().__init__(entity_description, coordinator)

    @staticmethod
    def _format_time(minutes: int) -> str:
        """Render minutes-since-midnight as a plain HH:MM wall-clock string.

        This is a time-of-day, not a timestamp - the device has no concept
        of a date here, so there's nothing to convert between UTC and local:
        it's displayed exactly as programmed, the same way HA's own
        Schedule helper shows recurring times.
        """
        hours, mins = divmod(int(minutes), 60)
        return f"{hours:02d}:{mins:02d}"

    def _format_transition(self, heating_setpoint: float) -> dict[str, Any]:
        """Split a transition's raw heating_setpoint into state plus temperature.

        For heat, the actual target comes back as a real number rather than
        folded into text; water has no real temperature so only state applies.
        """
        if self._zone == ZONE_WATER:
            if heating_setpoint == WATER_SCHEDULE_ON_SETPOINT:
                return {"state": "on"}
            if heating_setpoint == WATER_SCHEDULE_OFF_SETPOINT:
                return {"state": "off"}
            return {"state": "unrecognised", "raw_heating_setpoint": heating_setpoint}
        if heating_setpoint == WEEKLY_SCHEDULE_OFF_SETPOINT:
            return {"state": "off"}
        return {"state": "on", "temperature": heating_setpoint}

    @staticmethod
    def _ordered_days(schedule: dict[str, list[dict[str, Any]]]) -> list[str]:
        """Return the known days in Monday->Sunday order.

        The device reports days grouped by shared programme, in no particular
        order; presenting them as a fixed week reads far more naturally. Any
        day name the device sends that isn't a standard weekday is appended
        after, so nothing is silently dropped.
        """
        known = [day for day in VALID_SCHEDULE_DAYS if day in schedule]
        extra = [day for day in schedule if day not in VALID_SCHEDULE_DAYS]
        return known + extra

    def _format_schedule(
        self, schedule: dict[str, list[dict[str, Any]]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Convert the raw per-day transitions into readable time/state/temperature."""
        return {
            day: [
                {
                    "time": self._format_time(transition["time"]),
                    **self._format_transition(transition["heating_setpoint"]),
                }
                for transition in sorted(schedule[day], key=lambda t: t["time"])
            ]
            for day in self._ordered_days(schedule)
        }

    @staticmethod
    def _transition_label(transition: dict[str, Any]) -> str:
        """Render one already-formatted transition as compact human text."""
        if transition.get("state") == "on" and "temperature" in transition:
            return f"{transition['time']} {transition['temperature']}°C"
        return f"{transition['time']} {transition.get('state', '?')}"

    def _format_schedule_text(
        self, formatted: dict[str, list[dict[str, Any]]]
    ) -> dict[str, str]:
        """Render each day as a single readable line for a Markdown/entity card.

        e.g. "06:30 21°C · 09:00 off · 17:00 20°C · 22:00 off". The nested
        'schedule' attribute only shows as raw JSON in the UI; this is what a
        human actually reads at a glance.
        """
        return {
            day: " · ".join(self._transition_label(t) for t in transitions)
            if transitions
            else "no transitions"
            for day, transitions in formatted.items()
        }

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""

        schedule = (
            self.coordinator.weekly_schedule_water
            if self._zone == ZONE_WATER
            else self.coordinator.weekly_schedule_heat
        )

        if schedule:
            formatted = self._format_schedule(schedule)
            self._attr_native_value = f"{len(schedule)}/7 days known"
            self._attr_extra_state_attributes = {
                "schedule_text": self._format_schedule_text(formatted),
                "schedule": formatted,
                "raw_schedule": schedule,
            }
        else:
            self._attr_native_value = "unknown"
            self._attr_extra_state_attributes = {}

        self.async_write_ha_state()

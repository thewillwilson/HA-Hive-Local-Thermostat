"""Coordinator for Hive Local Thermostat integration."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast

from homeassistant.components.climate.const import (
    PRESET_BOOST,
    PRESET_NONE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.mqtt import client as mqtt_client
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import utcnow

from .const import (
    DEFAULT_FROST_TEMPERATURE,
    DEFAULT_HEATING_BOOST_MINUTES,
    DEFAULT_HEATING_BOOST_TEMPERATURE,
    DEFAULT_WATER_BOOST_MINUTES,
    DOMAIN,
    FROST_PROTECTION_SETPOINT,
    HIVE_BOOST,
    LOGGER,
    MODEL_SLR2,
    ZONE_HEAT,
    ZONE_WATER,
)

SCHEDULE_STORAGE_VERSION = 1

PRESET_MAP = {
    PRESET_NONE: "",
    PRESET_BOOST: HIVE_BOOST,
}

BOOST_ERROR = 65000


class HiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Hive data from MQTT."""

    current_temperature: float | None = None
    target_temperature: float | None = None
    preset_mode: str | None = None
    hvac_mode: HVACMode | None = None
    water_mode: str | None = None

    running_state_heat: str = ""
    running_state_water: str = ""

    heat_boost: bool = False
    water_boost: bool = False
    heat_boost_started: datetime | None = None
    water_boost_started: datetime | None = None
    heat_boost_started_duration: int = 0
    water_boost_started_duration: int = 0
    heat_boost_remaining: int = 0
    water_boost_remaining: int = 0

    pre_boost_hvac_mode: HVACMode | None = None
    pre_boost_occupied_heating_setpoint_heat: float | None = None
    pre_boost_water_mode: str | None = None

    # Number entity values
    heating_boost_duration: float = DEFAULT_HEATING_BOOST_MINUTES
    heating_boost_temperature: float = DEFAULT_HEATING_BOOST_TEMPERATURE
    heating_frost_prevention: float = DEFAULT_FROST_TEMPERATURE
    water_boost_duration: float = DEFAULT_WATER_BOOST_MINUTES

    # SLR2 pretend-off state (frost protection sent, but HA shows OFF)
    _user_set_off: bool = False
    # Temperature to restore when turning back on after a user-initiated OFF
    _pre_off_temperature: float | None = None
    # Last setpoint received with hold=False (genuine schedule target), used to suppress
    # stale hold=True echoes during SLR2 schedule-period transitions.
    _last_schedule_setpoint: float | None = None

    # Native weekly schedule, keyed by day name -> list of {"time": minutes, "heating_setpoint": temp}.
    # The device answers a schedule request in day-group chunks (one MQTT message per
    # group of days that share a programme), so this is accumulated across messages
    # rather than replaced wholesale on each one.
    weekly_schedule_heat: dict[str, list[dict[str, Any]]] | None = None
    weekly_schedule_water: dict[str, list[dict[str, Any]]] | None = None

    # Diagnostics
    last_mqtt_payload: dict[str, Any] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        model: str,
        topic: str,
        show_heat_schedule_mode: bool,  # noqa: FBT001
        show_water_schedule_mode: bool,  # noqa: FBT001
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN}_{entry_id}",
        )
        self.entry_id = entry_id
        self.model = model
        self.topic = topic
        self.show_heating_schedule_mode = show_heat_schedule_mode
        self.show_water_schedule_mode = show_water_schedule_mode
        self.data: dict[str, Any] = {}
        self._schedule_store: Store[dict[str, Any]] = Store(
            hass, SCHEDULE_STORAGE_VERSION, f"{DOMAIN}_{entry_id}_weekly_schedule"
        )

    async def async_load_weekly_schedule(self) -> None:
        """Load any weekly schedule persisted from a previous session.

        Call this before entities are added so they render last-known data
        immediately on startup, rather than sitting empty until a fresh
        get_weekly_schedule reply comes back over MQTT.
        """
        stored = await self._schedule_store.async_load()
        if stored:
            self.weekly_schedule_heat = stored.get("heat")
            self.weekly_schedule_water = stored.get("water")

    def _save_weekly_schedule(self) -> None:
        """Persist the current weekly schedule so it survives a restart.

        Fire-and-forget: this is called from the synchronous MQTT message
        callback, so the actual write is scheduled as a background task
        rather than awaited inline.
        """
        self.hass.async_create_task(
            self._schedule_store.async_save(
                {
                    "heat": self.weekly_schedule_heat,
                    "water": self.weekly_schedule_water,
                }
            ),
            f"{DOMAIN}_{self.entry_id}_save_weekly_schedule",
        )

    @property
    def topic_get(self) -> str:
        """Return the topic getter."""
        return self.topic + "/get"

    @property
    def topic_set(self) -> str:
        """Return the topic setter."""
        return self.topic + "/set"

    @property
    def boost_remaining_heat(self) -> int:
        """Return the remaining boost time for heating."""
        return self.heat_boost_remaining

    @property
    def boost_remaining_water(self) -> int:
        """Return the remaining boost time for water."""
        return self.water_boost_remaining

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self.running_state_heat == "preheating":
            return HVACAction.PREHEATING
        if self.running_state_heat == "heat":
            return HVACAction.HEATING
        if self.running_state_heat == "idle":
            return HVACAction.IDLE
        if self.running_state_heat == "off":
            return HVACAction.OFF
        return None

    @property
    def local_temperature_heat(self) -> float | None:
        """Return the local temperature for heating."""
        return self.current_temperature

    @property
    def pre_off_temperature(self) -> float | None:
        """Return the stored pre-off setpoint, if any."""
        return self._pre_off_temperature

    def climate_preset(self, mode: str) -> str:
        """Get the current preset."""
        return next(
            (k for k, v in PRESET_MAP.items() if v == mode), PRESET_MAP[PRESET_NONE]
        )

    @callback
    def handle_mqtt_message(self, message: ReceiveMessage) -> None:  # noqa: C901, PLR0912, PLR0915
        """Handle received MQTT message."""
        topic = message.topic
        payload = message.payload
        LOGGER.debug("Received from %s payload: %s", topic, payload)

        if not payload:
            LOGGER.error(
                "Received empty payload on topic %s, check that you have the correct topic name",
                topic,
            )
            return

        self.current_temperature = None
        self.target_temperature = None
        self.preset_mode = None
        self.hvac_mode = None
        self.heat_boost = False
        self.water_boost = False
        self.water_mode = None

        try:
            parsed_data: dict[str, Any] = json.loads(payload)

            # Store last payload for diagnostics
            self.last_mqtt_payload = parsed_data

            if not self.valid_data_for_model(parsed_data):
                return

            # Weekly schedule responses are only present when explicitly requested
            # (via async_get_weekly_schedule) and arrive as separate day-group
            # chunks, so merge rather than overwrite. Only persist when one
            # actually showed up in this message, not on every MQTT update.
            schedule_updated = False
            if self.model == MODEL_SLR2:
                if "weekly_schedule_heat" in parsed_data:
                    self.weekly_schedule_heat = self._merge_weekly_schedule(
                        self.weekly_schedule_heat, parsed_data["weekly_schedule_heat"]
                    )
                    schedule_updated = True
                if "weekly_schedule_water" in parsed_data:
                    self.weekly_schedule_water = self._merge_weekly_schedule(
                        self.weekly_schedule_water,
                        parsed_data["weekly_schedule_water"],
                    )
                    schedule_updated = True
            elif "weekly_schedule" in parsed_data:
                self.weekly_schedule_heat = self._merge_weekly_schedule(
                    self.weekly_schedule_heat, parsed_data["weekly_schedule"]
                )
                schedule_updated = True

            if schedule_updated:
                self._save_weekly_schedule()

            if self.model == MODEL_SLR2:
                reported_boost_remaining_heat = cast(
                    int,
                    parsed_data["temperature_setpoint_hold_duration_heat"]
                    if parsed_data["system_mode_heat"] == "emergency_heating"
                    else 0,
                )
                reported_boost_remaining_water = cast(
                    int,
                    parsed_data["temperature_setpoint_hold_duration_water"]
                    if parsed_data["system_mode_water"] == "emergency_heating"
                    else 0,
                )
                reported_boost_temperature = parsed_data[
                    "occupied_heating_setpoint_heat"
                ]
                self.running_state_heat = cast(
                    str,
                    parsed_data.get("running_state_heat")
                    if parsed_data.get("running_state_heat")
                    else "preheating",
                )
                self.running_state_water = cast(
                    str,
                    parsed_data.get("running_state_water")
                    if parsed_data.get("running_state_water")
                    else "preheating",
                )
                self.current_temperature = parsed_data["local_temperature_heat"]
                if parsed_data["occupied_heating_setpoint_heat"] == 1:
                    self.target_temperature = self.heating_frost_prevention
                else:
                    self.target_temperature = parsed_data[
                        "occupied_heating_setpoint_heat"
                    ]
                # When user-set OFF, the device reports the frost-protection setpoint.
                # Restore the pre-off temperature so HA displays and resumes correctly.
                if self._user_set_off and self._pre_off_temperature is not None:
                    self.target_temperature = self._pre_off_temperature

                # Guard against SLR2 schedule-transition race condition.
                # When the SLR2 transitions schedule periods it emits a burst of MQTT
                # messages. The genuine new setpoint arrives with hold=False; stale
                # echoes of the prior period's setpoint arrive with hold=True.
                # Strategy: track the last hold=False setpoint as the authoritative
                # target, and suppress any hold=True message that differs from it.
                # This catches echoes at any temperature (7°C frost, 19°C prior
                # setpoint, etc.) not just frost-level values.
                # Skip suppression when the user intentionally set OFF via HA —
                # that command also uses hold=True and must not be overridden.
                if parsed_data.get("temperature_setpoint_hold_heat") is False:
                    # Genuine schedule setpoint — record it as the authoritative target.
                    self._last_schedule_setpoint = self.target_temperature
                elif (
                    not self._user_set_off
                    and parsed_data.get("temperature_setpoint_hold_heat") is True
                    and self._last_schedule_setpoint is not None
                    and self.target_temperature != self._last_schedule_setpoint
                ):
                    LOGGER.warning(
                        "Suppressing stale setpoint echo during schedule transition "
                        "(ignoring %.1f°C, retaining %.1f°C)",
                        self.target_temperature,
                        self._last_schedule_setpoint,
                    )
                    self.target_temperature = self._last_schedule_setpoint

                self.preset_mode = self.climate_preset(parsed_data["system_mode_heat"])
                if parsed_data["system_mode_heat"] == "auto":
                    self.hvac_mode = HVACMode.AUTO
                if parsed_data["system_mode_heat"] == "heat":
                    if (
                        parsed_data["temperature_setpoint_hold_heat"] is False
                        and self.show_heating_schedule_mode
                    ):
                        self.hvac_mode = HVACMode.AUTO
                    else:
                        self.hvac_mode = HVACMode.HEAT
                if parsed_data["system_mode_heat"] == "emergency_heating":
                    self.hvac_mode = HVACMode.HEAT
                    self.heat_boost = True
                if parsed_data["system_mode_heat"] == "off":
                    self.hvac_mode = HVACMode.OFF
                if self._user_set_off:
                    self.hvac_mode = HVACMode.OFF

                if (
                    parsed_data["system_mode_heat"] != "emergency_heating"
                    and self.hvac_mode != HVACMode.OFF
                ):
                    self.pre_boost_occupied_heating_setpoint_heat = (
                        self.target_temperature
                    )
                    self.pre_boost_hvac_mode = self.hvac_mode
                if parsed_data["system_mode_water"] == "auto":
                    self.water_mode = "auto"
                if parsed_data["system_mode_water"] == "heat":
                    if parsed_data["temperature_setpoint_hold_water"] is False:
                        if self.show_water_schedule_mode:
                            self.water_mode = "auto"
                        else:
                            self.water_mode = "heat"
                    else:
                        self.water_mode = "heat"
                if parsed_data["system_mode_water"] == "emergency_heating":
                    self.water_mode = "boost"
                    self.water_boost = True
                if parsed_data["system_mode_water"] == "off":
                    self.water_mode = "off"

                if parsed_data["system_mode_water"] != "emergency_heating":
                    self.pre_boost_water_mode = self.water_mode
            else:
                reported_boost_remaining_heat = cast(
                    int,
                    parsed_data["temperature_setpoint_hold_duration"]
                    if parsed_data["system_mode"] == "emergency_heating"
                    else 0,
                )
                reported_boost_temperature = parsed_data["occupied_heating_setpoint"]
                self.running_state_heat = cast(
                    str,
                    parsed_data.get("running_state")
                    if parsed_data.get("running_state")
                    else "preheating",
                )
                self.current_temperature = parsed_data["local_temperature"]
                if parsed_data["occupied_heating_setpoint"] == 1:
                    self.target_temperature = self.heating_frost_prevention
                else:
                    self.target_temperature = parsed_data["occupied_heating_setpoint"]
                self.preset_mode = self.climate_preset(parsed_data["system_mode"])

                if parsed_data["system_mode"] == "auto":
                    self.hvac_mode = HVACMode.AUTO
                if parsed_data["system_mode"] == "heat":
                    if (
                        parsed_data["temperature_setpoint_hold"] is False
                        and self.show_heating_schedule_mode
                    ):
                        self.hvac_mode = HVACMode.AUTO
                    else:
                        self.hvac_mode = HVACMode.HEAT
                if parsed_data["system_mode"] == "emergency_heating":
                    self.hvac_mode = HVACMode.HEAT
                    self.heat_boost = True
                if parsed_data["system_mode"] == "off":
                    self.hvac_mode = HVACMode.OFF

                if parsed_data["system_mode"] != "emergency_heating":
                    self.pre_boost_occupied_heating_setpoint_heat = (
                        self.target_temperature
                    )
                    self.pre_boost_hvac_mode = self.hvac_mode

            if self.correct_heat_boost(
                reported_boost_remaining_heat, reported_boost_temperature
            ):
                return  # Correction made, exit to avoid state update loop
            self.record_heat_boost_state()

            if self.model == MODEL_SLR2:
                if self.correct_water_boost(reported_boost_remaining_water):
                    return  # Correction made, exit to avoid state update loop
                self.record_water_boost_state()

            self.async_set_updated_data(parsed_data)
        except json.JSONDecodeError:
            LOGGER.error("Failed to parse JSON from MQTT payload: %s", payload)
        except Exception as err:  # noqa: BLE001
            LOGGER.error("Error handling MQTT message: %s", err)

    @staticmethod
    def _merge_weekly_schedule(
        existing: dict[str, list[dict[str, Any]]] | None,
        response: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Merge one Get Weekly Schedule response (one or more days) into the store.

        The device answers with {"days": [...], "transitions": [...]}, covering
        whichever group of days shares that exact programme. A full-week request
        typically arrives as several of these, one per group, each landing as a
        separate MQTT message on the same key - so we accumulate by day rather
        than replace the whole store on every message.
        """
        store = dict(existing) if existing else {}
        days = response.get("days") or []
        transitions = response.get("transitions") or []
        for day in days:
            # Copy per day: the days in one response share an identical
            # programme, but they must not share the same list object (a later
            # in-place edit of one day would otherwise silently change them all).
            store[day] = list(transitions)
        return store

    def valid_data_for_model(self, data: dict[str, Any]) -> bool:
        """Check if data is valid for the current model."""
        if self.model == MODEL_SLR2:
            if "system_mode_water" not in data:
                LOGGER.error(
                    "Received data does not contain 'system_mode_water' for SLR2, check you have the correct model set"
                )
                return False
        elif "system_mode_water" in data:
            LOGGER.error(
                "Received data contains 'system_mode_water' for SLR1/OTR1, check you have the correct model set"
            )
            return False
        return True

    def correct_heat_boost(
        self, reported_boost_remaining_heat: int, reported_boost_temperature: float
    ) -> bool:
        """Check and correct boost remaining heat if necessary."""
        if reported_boost_remaining_heat > BOOST_ERROR:
            # Calculate remaining boost time based on when it started
            if self.heat_boost_started and self.heat_boost_started_duration > 0:
                elapsed = (utcnow() - self.heat_boost_started).total_seconds() / 60
                self.heat_boost_remaining = int(
                    self.heat_boost_started_duration - elapsed
                )
            else:
                self.heat_boost_remaining = 0

            LOGGER.warning(
                "Correcting reported boost remaining heat from %d to %d",
                reported_boost_remaining_heat,
                self.heat_boost_remaining,
            )
            if self.config_entry is not None:
                self.config_entry.async_create_task(
                    self.hass,
                    self.async_heating_boost(
                        self.heat_boost_remaining, reported_boost_temperature
                    ),
                )
            return True
        self.heat_boost_remaining = reported_boost_remaining_heat
        return False

    def correct_water_boost(self, reported_boost_remaining_water: int) -> bool:
        """Check and correct boost remaining water if necessary."""
        if reported_boost_remaining_water > BOOST_ERROR:
            # Calculate remaining boost time based on when it started
            if self.water_boost_started and self.water_boost_started_duration > 0:
                elapsed = (utcnow() - self.water_boost_started).total_seconds() / 60
                self.water_boost_remaining = int(
                    self.water_boost_started_duration - elapsed
                )
            else:
                self.water_boost_remaining = 0

            LOGGER.warning(
                "Correcting reported boost remaining water from %d to %d",
                reported_boost_remaining_water,
                self.water_boost_remaining,
            )
            if self.config_entry is not None:
                self.config_entry.async_create_task(
                    self.hass, self.async_water_boost(self.water_boost_remaining)
                )
            return True
        self.water_boost_remaining = reported_boost_remaining_water
        return False

    def record_heat_boost_state(self) -> None:
        """Record and track boost state for heating."""
        if self.heat_boost and self.heat_boost_remaining > 0:
            # Boost is active, record the start time if not already recorded
            if not self.heat_boost_started:
                self.heat_boost_started = utcnow()
                self.heat_boost_started_duration = self.heat_boost_remaining
        elif not self.heat_boost:
            # Boost is not active, clear tracking state
            self.heat_boost_started = None
            self.heat_boost_started_duration = 0

        if self.water_boost and self.water_boost_remaining > 0:
            # Water boost is active, record the start time if not already recorded
            if not self.water_boost_started:
                self.water_boost_started = utcnow()
                self.water_boost_started_duration = self.water_boost_remaining
        elif not self.water_boost:
            # Water boost is not active, clear tracking state
            self.water_boost_started = None
            self.water_boost_started_duration = 0

    def record_water_boost_state(self) -> None:
        """Record and track boost state for water."""
        if self.water_boost and self.water_boost_remaining > 0:
            # Water boost is active, record the start time if not already recorded
            if not self.water_boost_started:
                self.water_boost_started = utcnow()
                self.water_boost_started_duration = self.water_boost_remaining
        elif not self.water_boost:
            # Water boost is not active, clear tracking state
            self.water_boost_started = None
            self.water_boost_started_duration = 0

    async def _async_publish_set(self, payload: str) -> None:
        """Publish MQTT set message."""
        LOGGER.debug("Sending to %s message %s", self.topic_set, payload)
        await mqtt_client.async_publish(self.hass, self.topic_set, payload)

    async def _async_publish_get(self, payload: str) -> None:
        """Publish MQTT get (read request) message."""
        LOGGER.debug("Sending to %s message %s", self.topic_get, payload)
        await mqtt_client.async_publish(self.hass, self.topic_get, payload)

    def _weekly_schedule_key(self, zone: str) -> str:
        """Return the model-aware MQTT attribute key for the weekly schedule."""
        if self.model == MODEL_SLR2:
            return f"weekly_schedule_{zone}"
        return "weekly_schedule"

    async def async_get_weekly_schedule(self, zone: str = ZONE_HEAT) -> None:
        """Request the device report its native weekly schedule for a zone.

        The response arrives asynchronously (possibly as several messages, one
        per group of days) and is merged into weekly_schedule_heat/_water as it
        comes in - this call only triggers the read, it doesn't return the data.
        """
        if zone == ZONE_WATER and self.model != MODEL_SLR2:
            LOGGER.error("Water zone schedule is only available on SLR2")
            return

        key = self._weekly_schedule_key(zone)
        payload = json.dumps({key: ""})
        await self._async_publish_get(payload)

    async def async_set_weekly_schedule(
        self,
        zone: str,
        days: list[str],
        transitions: list[dict[str, Any]],
    ) -> None:
        """Write a native weekly schedule to the device for the given day(s).

        transitions is a list of {"time": <minutes since midnight>,
        "heating_setpoint": <value>} dicts (the shape read back from the
        device) - use WEEKLY_SCHEDULE_OFF_SETPOINT for a transition that should
        mean "off"/setback rather than a real target temperature.

        The write is translated into the shape Zigbee2MQTT's standard
        thermostat_weekly_schedule converter expects, which differs from what
        the device reports back: `days`->`dayofweek`, `time` (minutes)->
        `transitionTime` ("HH:MM"), `heating_setpoint`->`heatSetpoint` (the
        converter multiplies it by 100, matching the raw values the device
        reports). Verified against a real SLR2d.
        """
        if zone == ZONE_WATER and self.model != MODEL_SLR2:
            LOGGER.error("Water zone schedule is only available on SLR2")
            return

        z2m_transitions = [
            {
                "transitionTime": self._minutes_to_hhmm(transition["time"]),
                "heatSetpoint": transition["heating_setpoint"],
            }
            for transition in transitions
        ]
        key = self._weekly_schedule_key(zone)
        payload = json.dumps(
            {key: {"dayofweek": days, "transitions": z2m_transitions}}
        )
        await self._async_publish_set(payload)

    @staticmethod
    def _minutes_to_hhmm(minutes: int) -> str:
        """Render minutes-since-midnight as the "HH:MM" the converter accepts."""
        hours, mins = divmod(int(minutes), 60)
        return f"{hours:02d}:{mins:02d}"

    async def async_water_boost(
        self, boost_duration_minutes: int | None = None
    ) -> None:
        """Send water boost command."""

        self.pre_boost_water_mode = self.water_mode

        duration = str(int(boost_duration_minutes or self.water_boost_duration))
        payload = (
            r'{"system_mode_water":"emergency_heating","temperature_setpoint_hold_duration_water":'
            + duration
            + r',"temperature_setpoint_hold_water":1}'
        )

        self.water_boost = True
        self.water_boost_started = utcnow()
        self.water_boost_started_duration = int(duration)

        await self._async_publish_set(payload)

    async def async_water_boost_cancel(self) -> None:
        """Cancel water boost command."""

        if self.pre_boost_water_mode == "auto":
            await self.async_water_scheduled()
        elif self.pre_boost_water_mode == "heat":
            await self.async_water_always_on()
        else:
            await self.async_water_always_off()

    async def async_water_scheduled(self) -> None:
        """Send water scheduled command."""

        payload = r'{"system_mode_water":"auto"}'
        await self._async_publish_set(payload)

    async def async_water_always_on(self) -> None:
        """Send water always on command."""

        payload = r'{"system_mode_water":"heat","temperature_setpoint_hold_water":1}'
        await self._async_publish_set(payload)

    async def async_water_always_off(self) -> None:
        """Send water always off command."""

        payload = r'{"system_mode_water":"off","temperature_setpoint_hold_water":0}'
        await self._async_publish_set(payload)

    async def async_heating_boost(
        self,
        boost_duration_minutes: int | None = None,
        boost_temperature: float | None = None,
    ) -> None:
        """Send heating boost command."""

        self.pre_boost_occupied_heating_setpoint_heat = self.target_temperature
        self.pre_boost_hvac_mode = self.hvac_mode

        duration = str(int(boost_duration_minutes or self.heating_boost_duration))
        temperature = str(boost_temperature or self.heating_boost_temperature)

        if self.model == MODEL_SLR2:
            payload = (
                r'{"system_mode_heat":"emergency_heating","temperature_setpoint_hold_duration_heat":'
                + duration
                + r',"temperature_setpoint_hold_heat":1,"occupied_heating_setpoint_heat":'
                + temperature
                + r"}"
            )
        else:
            payload = (
                r'{"system_mode":"emergency_heating","temperature_setpoint_hold_duration":'
                + duration
                + r',"temperature_setpoint_hold":1,"occupied_heating_setpoint":'
                + temperature
                + r"}"
            )

        self.heat_boost = True
        self.heat_boost_started = utcnow()
        self.heat_boost_started_duration = int(duration)

        await self._async_publish_set(payload)

    async def async_heating_boost_cancel(self) -> None:
        """Cancel heating boost command."""

        if self.pre_boost_hvac_mode == HVACMode.AUTO:
            await self.async_set_hvac_mode_auto()
        elif self.pre_boost_hvac_mode == HVACMode.HEAT:
            if self.pre_boost_occupied_heating_setpoint_heat is not None:
                await self.async_set_hvac_mode_heat(
                    self.pre_boost_occupied_heating_setpoint_heat
                )
            else:
                await self.async_set_hvac_mode_heat(self.heating_frost_prevention)
        else:
            await self.async_set_hvac_mode_off()

    async def async_set_temperature(self, temperature: float) -> None:
        """Set temperature."""

        # For SLR2 while user-set OFF, the device is in frost-protection HEAT mode.
        # Sending a setpoint would cause it to start heating immediately, so only
        # store the temperature for resume and do not publish to the device.
        if self._user_set_off and self.model == MODEL_SLR2:
            self._pre_off_temperature = temperature
            return

        if self.model == MODEL_SLR2:
            payload = r'{"occupied_heating_setpoint_heat":' + str(temperature) + r"}"
        else:
            payload = r'{"occupied_heating_setpoint":' + str(temperature) + r"}"

        # If the user changes the setpoint while off (SLR1/OTR1), track it as the new resume temperature
        if self._user_set_off:
            self._pre_off_temperature = temperature

        await self._async_publish_set(payload)

    async def async_set_hvac_mode_off(self) -> None:
        """Set HVAC mode to off."""

        if self.model == MODEL_SLR2:
            payload = (
                r'{"system_mode_heat":"heat","temperature_setpoint_hold_heat":true'
                r',"occupied_heating_setpoint_heat":'
                + str(FROST_PROTECTION_SETPOINT)
                + r"}"
            )
        else:
            payload = r'{"system_mode":"off","temperature_setpoint_hold":false}'

        # Save the current setpoint so it can be restored when turning back on
        if self.target_temperature is not None:
            self._pre_off_temperature = self.target_temperature
        self._user_set_off = True
        self.hvac_mode = HVACMode.OFF
        await self._async_publish_set(payload)

    async def async_set_hvac_mode_auto(self) -> None:
        """Set HVAC mode to auto."""

        if self.model == MODEL_SLR2:
            payload = r'{"system_mode_heat":"auto"}'
        else:
            payload = r'{"system_mode":"auto"}'

        self._user_set_off = False
        self.hvac_mode = HVACMode.AUTO
        await self._async_publish_set(payload)

    async def async_set_hvac_mode_heat(
        self,
        temperature: float,
        set_from_temperature: bool = False,  # noqa: ARG002, FBT001, FBT002
    ) -> None:
        """Set HVAC mode to heat."""

        if self.model == MODEL_SLR2:
            payload = (
                r'{"system_mode_heat":"heat","occupied_heating_setpoint_heat":'
                + str(temperature)
                + r',"temperature_setpoint_hold_heat":true,"temperature_setpoint_hold_duration_heat":0}'
            )
        else:
            payload = (
                r'{"system_mode":"heat","occupied_heating_setpoint":'
                + str(temperature)
                + r',"temperature_setpoint_hold":true,"temperature_setpoint_hold_duration":0}'
            )

        self._user_set_off = False
        self._pre_off_temperature = None
        self.hvac_mode = HVACMode.HEAT
        await self._async_publish_set(payload)

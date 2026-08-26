"""Constants for Hive Local Thermostat."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

MIN_HA_VERSION = "2025.4"

DOMAIN = "hive_local_thermostat"
CONFIG_VERSION = 1

CONF_MQTT_TOPIC = "mqtt_topic"
CONF_MODEL = "model"
CONF_SHOW_HEAT_SCHEDULE_MODE = "show_heat_schedule_mode"
CONF_SHOW_WATER_SCHEDULE_MODE = "show_water_schedule_mode"

MODEL_OTR1 = "OTR1"
MODEL_SLR1 = "SLR1"
MODEL_SLR2 = "SLR2"

MODELS = [
    MODEL_OTR1,
    MODEL_SLR1,
    MODEL_SLR2,
]

HIVE_BOOST = "emergency_heat"

DEFAULT_FROST_TEMPERATURE = 7
FROST_PROTECTION_SETPOINT = 7
DEFAULT_HEATING_BOOST_MINUTES = 120
DEFAULT_HEATING_BOOST_TEMPERATURE = 25
DEFAULT_WATER_BOOST_MINUTES = 60

MAXIMUM_BOOST_MINUTES = 180

# Weekly schedule (native device programme, read/written over the Zigbee
# hvacThermostat cluster's Get/Set Weekly Schedule commands via Z2M)
ZONE_HEAT = "heat"
ZONE_WATER = "water"

VALID_SCHEDULE_DAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

# Most transitions the device accepts per day for a weekly schedule; also the
# number of editable rows the options-flow schedule wizard offers.
MAX_SCHEDULE_TRANSITIONS = 6

# Sentinel heating_setpoint value the device uses in a transition to mean
# "no target here" (i.e. off/setback), rather than a real temperature.
WEEKLY_SCHEDULE_OFF_SETPOINT = 1

# The water zone has no real temperature dial (it's an on/off relay), so the
# device reuses the same heating_setpoint field with its own pair of sentinel
# values instead - confirmed against a real SLR2d rather than assumed.
WATER_SCHEDULE_ON_SETPOINT = 99
WATER_SCHEDULE_OFF_SETPOINT = 0

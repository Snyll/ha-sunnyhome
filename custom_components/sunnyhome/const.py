"""Constants of the SunnyHome integration."""

DOMAIN = "sunnyhome"
VERSION = "0.20.0"  # manifest.json's version, sent on every claim (RA-004; HA-014: SunnyHome sends heat commands only to 0.16.0 or later; WALLBOX-005: set_current only to 0.17.0 or later; HA-017: fan / swing commands only for lists a 0.19.0+ component reported; HA-021: select, number, preset and range commands only to 0.20.0 or later)
DEFAULT_URL = "https://sunnyhome.cloud"
CONF_LINK_ID = "link_id"
CONF_ENTITIES = "entities"
CONF_CONTROL = "control"
CONF_SENSORS = "sensors"
CONF_BINARY = "binary_sensors"  # HA-012
CONF_PEOPLE = "people"  # HA-012
CONF_HEAT = "heat"  # HA-013: climate and water_heater entities
CONF_DEVICES = "devices"  # HA-021: whole HA devices (device registry ids), shared with their control (D2)
CONF_INVERTER = "inverter_control"
CONF_WALLBOX = "wallbox_control"  # EV-004
# WALLBOX-005: the Other wallbox options (a charger's power sensor and current number are required together)
CONF_OW_POWER = "other_wallbox_power"
CONF_OW_CURRENT = "other_wallbox_current"
CONF_OW_SWITCH = "other_wallbox_switch"
CONF_OW_ENERGY = "other_wallbox_energy"
CONF_OW_STATUS = "other_wallbox_status"
CONF_OTHER_WALLBOX = "other_wallbox_control"  # the control opt-in

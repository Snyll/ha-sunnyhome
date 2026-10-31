"""Pure helpers for the SunnyHome device API (stdlib only, no Home Assistant imports)."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

# solax_modbus sensor keys sent to SunnyHome (profile v2: SRC-005 adds the energy counters). Pinned to
# wills106/homeassistant-solax-modbus custom_components/solax_modbus/plugin_solax.py
# at tag 2026.09.4; the key is the entity unique_id minus the "<hub name>_" prefix.
PROFILE_KEYS = frozenset({
    "pv_power_1", "pv_power_2", "pv_power_3", "pv_power_total",
    "measured_power", "inverter_power",
    "inverter_power_l1", "inverter_power_l2", "inverter_power_l3",
    "eps_power_active_l1", "eps_power_active_l2", "eps_power_active_l3",
    "inverter_temperature", "run_mode",
    "battery_power_charge", "battery_capacity", "battery_temperature",
    "today_s_solar_energy", "total_solar_energy", "today_s_yield", "total_yield",
    "today_s_import_energy", "today_s_export_energy",
    "battery_input_energy_today", "battery_output_energy_today",
    # SRC-008 (0.7.1): grid-to-battery energy today (input register 0x91); disabled by default in solax_modbus, sent once enabled
    "e_charge_today",
    # BATT2-006 (0.12.0): pack 1/2 of dual-battery models (GEN5 X3-Ultra, GEN6 X3-Hybrid-G4 Pro and X1-VAST; plugin_solax.py at
    # main 1c6f72f); there battery_power_charge and battery_capacity are already the totals and battery_temperature does not exist
    "battery_1_voltage_charge", "battery_1_current_charge", "battery_1_power_charge", "battery_1_capacity_charge", "battery_1_temperature",
    "battery_2_voltage_charge", "battery_2_current_charge", "battery_2_power_charge", "battery_2_capacity_charge", "battery_2_temperature",
    # HA-022 (0.21.0): the BMS readings (GEN4-6; plugin_solax.py at 2026.10.2) and the battery voltage and current (GEN2-4)
    "cell_voltage_high", "cell_voltage_low", "battery_voltage_cell_difference", "battery_temp_high", "battery_temp_low",
    "battery_soh", "battery_voltage_charge", "battery_current_charge",
})

# SRC-009 (0.7.2): profile sensors solax_modbus ships disabled that SunnyHome turns on itself (exact home consumption; HA-022
# (0.21.0): the BMS readings).
AUTO_ENABLE_KEYS = frozenset({
    "e_charge_today",
    "cell_voltage_high", "cell_voltage_low", "battery_voltage_cell_difference", "battery_temp_high", "battery_temp_low", "battery_soh",
})

# SMB-004 (0.8.0): the Growatt, Solis and Sofar hybrids of solax_modbus's plugin_growatt.py, plugin_solis.py (and
# plugin_solis_fb00.py, the same sensor map) and plugin_sofar.py at 2026.10.1 (d644181), docs/26-solax-modbus-plugins.md section 3.
# Kept equal to the server's catalog paths (frontend/src/<brand>Metrics.json) by tests/test_payload.py; both Growatt families
# (GEN3, GEN4) and both Sofar off-grid L1 keys (X3, X1) are listed, as a hub has only its own.
GROWATT_KEYS = frozenset({
    "pv_power_total", "pv_power_1", "pv_power_2", "pv_power_3",
    "total_reverse_power", "total_forward_power", "ac_power_to_grid_total", "ac_power_to_user_total",
    "output_power", "inverter_temperature", "battery_combined_power", "battery_soc", "battery_temperature",
    "today_s_solar_energy", "total_solar_energy", "today_s_power_generation", "total_power_generation",
    "today_s_grid_import", "today_s_grid_export", "today_s_battery_input_energy", "today_s_battery_output_energy",
    "today_s_yield", "today_s_load", "total_load_power", "total_house_load", "run_mode",
})
SOLIS_KEYS = frozenset({
    "pv_total_power", "pv_power_1", "pv_power_2", "pv_power_3", "meter_active_power", "active_power", "bypass_load",
    "inverter_temperature", "battery_input_energy", "battery_output_energy", "battery_soc",
    "power_generation_today", "power_generation_total", "grid_import_today", "grid_export_today",
    "battery_charge_today", "battery_discharge_today", "house_load_today", "house_load", "inverter_status",
})
SOFAR_KEYS = frozenset({
    "pv_power_total", "pv_power_1", "pv_power_2", "pv_power_3", "active_power_pcc_total", "active_power_output_total",
    "active_power_output_l1", "active_power_output_l2", "active_power_output_l3",
    "offgrid_active_power_output_l1", "offgrid_active_power_output_l2", "offgrid_active_power_output_l3", "offgrid_active_power_output",
    "inverter_temperature_1", "battery_power_total", "battery_capacity_total", "battery_temperature_1",
    "solar_generation_today", "solar_generation_total", "import_energy_today", "export_energy_today",
    "battery_input_energy_today", "battery_output_energy_today", "load_consumption_today", "active_power_load_sys", "system_state",
})
BRAND_KEYS = {"growatt": GROWATT_KEYS, "solis": SOLIS_KEYS, "solis_fb00": SOLIS_KEYS, "sofar": SOFAR_KEYS}
# The sensors each brand plugin ships disabled that its profile needs (SRC-009 pattern): the Growatt serial (the device serial is
# often the firmware string) and GEN4 PV total; the Solis battery direction and, on solis_fb00, the battery power magnitude that the
# plugin's battery_input_energy / battery_output_energy pair is computed from.
BRAND_AUTO_ENABLE = {
    # SMB-007 (0.9.0): also the read-only readback sensors of Growatt GEN4 time slots 2-9 (slot 1's are on by default), so the
    # settings show the whole schedule; never a writable entity.
    "growatt": frozenset({"serialnumber", "total_solar_energy"} | {
        f"time_{n}_{part}_read" for n in range(2, 10) for part in ("begin", "end", "mode", "enabled")}),
    "solis": frozenset({"battery_charge_direction"}),
    "solis_fb00": frozenset({"battery_charge_direction", "battery_power"}),
}
_NO_SERIAL = frozenset({"", "unknown", "unavailable"})


# SMB-007 (0.9.0): the settings profile of a brand hub (the server's HA-007 row id); its settings are only read, like SolaX ones.
SETTINGS_PROFILES = {"growatt": "solax_modbus_growatt", "solis": "solax_modbus_solis", "solis_fb00": "solax_modbus_solis",
                     "sofar": "solax_modbus_sofar"}


def settings_integration(plugin: str | None) -> str | None:
    """SMB-007: the settings profile a solax_modbus hub of this plugin sends under: solax_modbus for a SolaX plugin, the brand's
    profile for Growatt, Solis and Sofar, None for any other plugin (no settings)."""
    return "solax_modbus" if solax_plugin(plugin) else SETTINGS_PROFILES.get(plugin)


def solax_plugin(plugin: str | None) -> bool:
    """HA-008: a SolaX inverter plugin's hub (or one whose plugin is unknown, as before 0.7.3); only it gets remote and settings."""
    return plugin is None or plugin.startswith("solax") and not plugin.startswith("solax_ev")


def hub_keys(plugin: str | None) -> frozenset[str]:
    """SMB-004: the keys sent for a hub of this plugin: its brand's, else the SolaX profile (another plugin is refused by SunnyHome)."""
    return BRAND_KEYS.get(plugin, PROFILE_KEYS)


def auto_enable(key: str, disabled_by: str | None, plugin: str | None = None) -> bool:
    """Whether to enable this hub sensor: only one solax_modbus itself disabled, never one the user turned off; e_charge_today on a
    SolaX plugin's hub only (solinteg also has it), a brand's own list on its hub (SMB-004)."""
    wanted = AUTO_ENABLE_KEYS if solax_plugin(plugin) else BRAND_AUTO_ENABLE.get(plugin, frozenset())
    return key in wanted and disabled_by == "integration"


def ident(value: object) -> str | None:
    """SMB-004: a brand hub's serial or family as the server bounds it (1-64 printable characters, not unknown or unavailable),
    else None: a longer one would make the server refuse the whole post."""
    return value if isinstance(value, str) and value not in _NO_SERIAL and _utf16(value) <= MAX_NAME and value.isprintable() else None

# GW-002 (0.7.0): the Home Assistant core goodwe integration's sensor ids SunnyHome maps (HomeAssistantGoodweMapping, GoodWe ET,
# EH, BT and BH: goodwe==0.4.10, the et reader); the id is the <id> of unique_id goodwe-<id>-<serial>. Kept equal to the server's
# GoodWe catalog paths by tests/test_payload.py. ppv4 has no catalog name and is not sent.
GOODWE_KEYS = frozenset({
    "ppv", "ppv1", "ppv2", "ppv3", "active_power", "total_inverter_power", "pgrid", "pgrid2", "pgrid3",
    "backup_p1", "backup_p2", "backup_p3", "temperature", "work_mode",
    "pbattery1", "battery_soc", "battery_temperature",
    "e_day", "e_total", "e_day_imp", "e_day_exp", "e_bat_charge_day", "e_bat_discharge_day", "e_load_day",
})

# DEYE-003 (0.10.0): the ha-solarman deye_p3 / deye_hybrid sensor keys SunnyHome maps (davidrapan/ha-solarman ac1d88b,
# docs/21-deye.md section 4); the key is the sensor unique_id <entry id>_<key>_sensor minus prefix and suffix. Kept equal to the
# server's Deye catalog paths by tests/test_payload.py.
DEYE_KEYS = frozenset({
    "pv_power", "pv1_power", "pv2_power", "pv3_power", "pv4_power", "grid_power", "power", "load_power",
    "battery_power", "battery", "battery_temperature", "temperature",
    "today_production", "total_production", "today_energy_import", "today_energy_export",
    "today_battery_charge", "today_battery_discharge", "today_load_consumption", "device_state",
})
_DEYE_SERIAL = re.compile(r"[A-Za-z0-9]{6,32}")


def deye_manufacturer(manufacturer: object) -> bool:
    """DEYE-003: a Deye inverter device of ha-solarman (manufacturer "Deye"); HA core's Solarman plugs and meters say "SOLARMAN"."""
    return isinstance(manufacturer, str) and manufacturer.lower().startswith("deye")


def deye_serial(value: object) -> str | None:
    """DEYE-003: the Device sensor's Serial Number attribute, trimmed, when it is 6-32 letters and digits; else None."""
    text = "" if value is None else str(value).strip()
    return text if _DEYE_SERIAL.fullmatch(text) else None


def deye_key(unique_id: str, entry_id: str) -> str | None:
    """DEYE-003: the key of a solarman sensor's unique_id slugify(entry_id, key, "sensor"); None for another shape or entry.
    ponytail: entry ids are ULIDs (letters and digits), so lowercasing equals ha-solarman's slugify of them."""
    prefix, suffix = f"{entry_id.lower()}_", "_sensor"
    if len(unique_id) <= len(prefix) + len(suffix) or not unique_id.startswith(prefix) or not unique_id.endswith(suffix):
        return None
    return unique_id[len(prefix):-len(suffix)]


MAX_READINGS = 64
MAX_ENTITIES = 64
MAX_NAME = 64
STATES = frozenset({"on", "off", "unavailable", "unknown"})
# The server's entity id rule; one entity outside it would make the server refuse the whole post.
_ENTITY_ID = re.compile(r"(switch|light)\.[a-z0-9_]{1,200}")
# HA-012 (0.14.0): picked binary sensors and people ride in the same "entities" list (kind "binary" / "person"), read only and never in the
# command or lease rules above. The id bound keeps the whole id within the server's 207 characters ("binary_sensor." is 14).
BINARY_CLASSES = frozenset({"door", "window", "garage_door", "opening", "motion", "occupancy"})
# HA-013 (0.15.0): picked heat pumps and boilers (climate, water_heater) ride there too (kind = the domain, plus a "heat" object), read only; "climate." is 8
# characters and "water_heater." 13, so their ids stop at 199 and 194. Everything in "heat" is sanitised here, because one item the server refuses refuses the post.
HEAT_DOMAINS = frozenset({"climate", "water_heater"})
MAX_HEAT_TEXT = 32  # a mode, an action or the state (UTF-16 code units, like the server)
MAX_HEAT_MODES = 8
# HA-017 (0.19.0): a climate's fan speed and vertical / horizontal vane (HA attribute, list attribute, our value key, our list key); each pair is sent only
# when HA reports the list, at most MAX_AIR_MODES distinct values (MELCloud's horizontal vane has 8). The server refuses a half pair or one on a water heater.
AIR = (("fan_mode", "fan_modes", "fanMode", "fanModes"), ("swing_mode", "swing_modes", "swingMode", "swingModes"),
       ("swing_horizontal_mode", "swing_horizontal_modes", "swingHorizontalMode", "swingHorizontalModes"))
AIR_LISTS = frozenset(item[1] for item in AIR)
MAX_AIR_MODES = 16
HEAT_LIMIT = 1000  # a number beyond it is not a temperature
# HA-014 (0.16.0): the heat entities a command may name (the same id bounds as the readings above).
_HEAT_ENTITY = re.compile(r"climate\.[a-z0-9_]{1,199}|water_heater\.[a-z0-9_]{1,194}")
_STATE_ID = re.compile(r"(switch|light)\.[a-z0-9_]{1,200}|binary_sensor\.[a-z0-9_]{1,193}|person\.[a-z0-9_]{1,200}"
                       r"|climate\.[a-z0-9_]{1,199}|water_heater\.[a-z0-9_]{1,194}|(select|number)\.[a-z0-9_]{1,200}")
# HA-021 (0.20.0): the shape a server that answers "contract":2 takes (HA-019). Whole devices add selects and numbers (their own kinds), the HA device
# id and model, a config entity's category, a sensor's state class and Energy dashboard role, and a climate's humidity, presets and range targets.
SETTING_DOMAINS = frozenset({"select", "number"})
_SETTING_ENTITY = re.compile(r"(select|number)\.[a-z0-9_]{1,200}")
MAX_DEVICE_ENTITIES = MAX_DEVICE_SENSORS = 512  # the server's caps for the 0.20.0 shape (HomeAssistantEntities.MAXIMUM, SENSORS)
# The server inflates at most 2 MiB and takes at most 256 KiB compressed (HA-019 post review; the spec's 512 KiB was lowered): a body stays under both.
GZIP_BUDGET, JSON_BUDGET = 240 * 1024, 2_000_000
MAX_OPTIONS, MAX_OPTION_TEXT = 32, 64
NUMBER_LIMIT = 1_000_000
MAX_PRESETS = 16
PRESET = ("preset_mode", "preset_modes", "presetMode", "presetModes")
V2_ATTRS = frozenset({"current_humidity", "preset_modes", "target_temp_low", "target_temp_high"})  # left out of a 0.19.0-shape post
_DEVICE_ID = re.compile(r"[0-9a-f]{32}")
ANY_CLASS = re.compile(r"[a-z0-9_]{1,32}")
STATE_CLASSES = frozenset({"measurement", "total", "total_increasing"})
ENERGY_ROLES = ("grid", "solar", "battery")
# HA-005: picked sensors. The server's rules again (one item outside them refuses the whole post): the id within the rule bound
# (64 characters), these device classes (HA-011: battery, voltage, current, humidity, gas and water added; gas and water are meter totals like
# energy), at most 32, state and unit lengths counted like the server (UTF-16 code units).
MAX_SENSORS = 32
MAX_STATE = 64
MAX_UNIT = 16
SENSOR_CLASSES = frozenset({"temperature", "power", "energy", "battery", "voltage", "current", "humidity", "gas", "water"})
SENSOR_ID = re.compile(r"sensor\.[a-z0-9_]{1,57}")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "host.docker.internal"})
# ISET-004 (0.5.0): the inverter settings of the hub, read only. Which entities count is the mapping profile's business: the
# component ships copies of the server's Home Assistant profiles (profiles/home_assistant.<integration>.json, kept equal by
# tests/test_payload.py) and sends the state of the entities a profile names, at most MAX_SETTINGS, outside MAX_READINGS.
MAX_SETTINGS = 64
# SMB-007: a mapping names one entity, a list of them (one per model family) or an hour and a minute number ("hourMinute").
def _entities(mapping: dict) -> list[str]:
    named = mapping.get("entity", mapping.get("hourMinute", []))
    return [named] if isinstance(named, str) else list(named)


SETTINGS_ENTITIES = {
    path.name[len("home_assistant."):-len(".json")]: frozenset(
        e for m in json.loads(path.read_text("utf-8"))["settings"].values() for e in _entities(m))
    for path in sorted((Path(__file__).parent / "profiles").glob("home_assistant.*.json"))
}
_CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{8}")


def normalize_code(text: str) -> str | None:
    """Return the 8-character Crockford base32 pairing code, or None if invalid."""
    code = re.sub(r"[\s-]", "", text.upper()).translate(str.maketrans("OIL", "011"))
    return code if _CODE.fullmatch(code) else None


def goodwe_key(unique_id: str, serial: str) -> str | None:
    """GW-002: the sensor id of a goodwe entity's unique_id goodwe-<id>-<serial>; None for another shape or serial."""
    prefix, suffix = "goodwe-", f"-{serial}"
    if len(unique_id) <= len(prefix) + len(suffix) or not unique_id.startswith(prefix) or not unique_id.endswith(suffix):
        return None
    return unique_id[len(prefix):-len(suffix)]


def valid_base_url(url: str) -> bool:
    """https is required; http only for local development hosts."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if not host:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Entity(NamedTuple):
    """A shared switch or light, as read from the HA state machine. HA-012: also a binary sensor (with its device class) or a person."""

    entity_id: str
    name: str | None
    state: str
    brightness: int | None  # HA scale 0-255
    supports_brightness: bool
    last_changed: datetime | None
    device_class: str | None = None  # HA-012: a binary sensor's class
    attributes: Mapping | None = None  # HA-013: a climate's or water heater's state attributes (read by heat(), nothing else leaves)
    unit: str | None = None  # HA-013: the temperature unit of the HA instance (hass.config.units.temperature_unit); climate entities carry none
    device: str | None = None  # HA-016: the name of the entity's HA device (none for a person)
    maker: str | None = None  # HA-016: that device's manufacturer
    device_id: str | None = None  # HA-021: the HA device registry id (only for a 0.20.0-shape post)
    model: str | None = None  # HA-021: that device's model
    category: str | None = None  # HA-021: "config" for a config entity (only for a 0.20.0-shape post)


def _short(text: object) -> str | None:
    """A device name or maker within the server's name rule (printable, trimmed, 1-64 UTF-16 units), else None."""
    text = text.strip() if isinstance(text, str) else ""
    return text if text and text.isprintable() and _utf16(text) <= MAX_NAME else None


def _device(device: str | None, maker: str | None, device_id: str | None = None, model: str | None = None) -> dict:
    """HA-016: "deviceName" and "manufacturer" of an entity's HA device, only when it has a usable name (the picker groups by it).
    HA-021: "deviceId" whenever the entity has a device, whatever its name, and "model" only with it."""
    name = _short(device)
    item = {"deviceName": name, "manufacturer": _short(maker)} if name else {}
    if isinstance(device_id, str) and _DEVICE_ID.fullmatch(device_id):
        item["deviceId"] = device_id
        if model := _short(model):
            item["model"] = model
    return item


def _option_text(value: object) -> str | None:
    """HA-021: a select option (or its state) as the server bounds it: printable text of 1-64 UTF-16 units, else None."""
    return value if isinstance(value, str) and value and value.isprintable() and _utf16(value) <= MAX_OPTION_TEXT else None


def _number(value: object) -> float | None:
    """HA-021: a number entity's value, bound or step: finite and within ±NUMBER_LIMIT (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > NUMBER_LIMIT or not math.isfinite(value):
        return None
    return float(value)


def setting(domain: str, state: str, attributes: Mapping | None) -> dict:
    """HA-021: what a select or number shows, sanitised to the server's rules: a select's option and options (at most MAX_OPTIONS distinct), a number's
    value, min, max and step (a range with min > max or a step <= 0 is None: such a number is neither sent nor commanded). Also the Seen of a command."""
    live = state not in ("unavailable", "unknown")
    attributes = attributes or {}
    if domain == "select":
        listed = attributes.get("options")
        options = list(dict.fromkeys(o for o in listed if _option_text(o)))[:MAX_OPTIONS] if isinstance(listed, (list, tuple)) else []
        return {"option": _option_text(state) if live else None, "options": options}
    low, high, step = (_number(attributes.get(key)) for key in ("min", "max", "step"))
    if low is None or high is None or low > high:
        low = high = None
    try:
        value = _number(float(state)) if live else None
    except (TypeError, ValueError):
        value = None
    return {"value": value, "min": low, "max": high, "step": step if step is not None and step > 0 else None}


def energy_roles(prefs: object) -> dict[str, str]:
    """HA-021 (D6): {statistic id: grid | solar | battery} of the Energy dashboard's sources (stat_energy_* and stat_rate*: energy and power, never money);
    the caller matches entity ids only, so an external statistic ("shelly:...") never matches."""
    roles: dict[str, str] = {}

    def walk(node: object, role: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and key.startswith(("stat_energy_", "stat_rate")):
                    roles.setdefault(value, role)
                else:
                    walk(value, role)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, role)
    sources = prefs.get("energy_sources") if isinstance(prefs, Mapping) else None
    for source in sources if isinstance(sources, (list, tuple)) else ():
        if isinstance(source, Mapping) and source.get("type") in ENERGY_ROLES:
            walk(source, source["type"])
    return roles


def _name(name: str | None, entity_id: str) -> str:
    """The server's name rule: printable, trimmed, at most 64 characters; else the entity id."""
    name = (name or "").strip()
    return ((name if name.isprintable() else "") or entity_id)[:MAX_NAME].rstrip()


def _person_state(state: str) -> str:
    """HA-012: a person is home or away, nothing else: a zone name is away, so no place name leaves Home Assistant."""
    return state if state in ("home", "unavailable", "unknown") else "away"


def _heat_number(value: object) -> float | None:
    """HA-013: a finite number of at most HEAT_LIMIT in size; a bool, text, NaN or infinity is not a reading (null, shown as a dash, never zero)."""
    # The size test comes first: math.isfinite overflows on a huge int.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or abs(value) > HEAT_LIMIT or not math.isfinite(value):
        return None
    return float(value)


def _heat_text(value: object) -> str | None:
    """HA-013: printable text of 1-MAX_HEAT_TEXT characters (a mode, an action), else None."""
    return value if isinstance(value, str) and value and value.isprintable() and _utf16(value) <= MAX_HEAT_TEXT else None


def heat(domain: str, state: str, attributes: Mapping | None, unit: str | None) -> dict:
    """HA-013: the "heat" object of a climate or water_heater: current and target temperature, the target range and step, the mode (the state,
    which is hvac_mode or the operation mode), the available modes (at most MAX_HEAT_MODES), a climate's hvac_action and the unit. Sanitised so the
    server never refuses it: a bad number is None, a range with min > max loses both ends, a step <= 0 is the default (0.5 for °C, 1 for °F), a
    mode that is not printable text of at most 32 characters is dropped. An unavailable or unknown entity reports no values at all. A dual setpoint
    (target_temp_low and target_temp_high without a temperature) has no target here; HA-021 reports and controls it through targetLow/targetHigh."""
    live = state not in ("unavailable", "unknown")
    attributes = attributes if live and attributes else {}
    unit = unit if unit in ("°C", "°F") else "°C"
    low, high = _heat_number(attributes.get("min_temp")), _heat_number(attributes.get("max_temp"))
    if low is not None and high is not None and low > high:
        low = high = None
    step = _heat_number(attributes.get("target_temp_step"))
    listed = attributes.get("hvac_modes" if domain == "climate" else "operation_list")
    modes = [m for m in listed if _heat_text(m)][:MAX_HEAT_MODES] if isinstance(listed, (list, tuple)) else []
    mode = _heat_text(state if domain == "climate" else attributes.get("operation_mode", state)) if live else None
    result = {
        "current": _heat_number(attributes.get("current_temperature")),
        "target": _heat_number(attributes.get("temperature")),
        "min": low, "max": high, "step": step if step is not None and step > 0 else 0.5 if unit == "°C" else 1.0,
        "mode": mode, "modes": modes,
        "action": _heat_text(attributes.get("hvac_action")) if domain == "climate" else None,
        "unit": unit,
    }
    for attr, list_attr, key, list_key in (*AIR, PRESET) if domain == "climate" else ():  # HA-017: only the pairs HA reports a usable list for (HA-021: presets too)
        listed = attributes.get(list_attr)
        values = list(dict.fromkeys(m for m in listed if _heat_text(m)))[:MAX_AIR_MODES] if isinstance(listed, (list, tuple)) else []
        if values:
            result[key], result[list_key] = _heat_text(attributes.get(attr)), values
    if domain == "climate":  # HA-021: the humidity (0-100) and the range targets (both, low <= high), only when HA reports them (the caller strips them for 0.19.0)
        humidity = _heat_number(attributes.get("current_humidity"))
        if humidity is not None and 0 <= humidity <= 100:
            result["humidity"] = humidity
        if "target_temp_low" in attributes and "target_temp_high" in attributes:
            low, high = _heat_number(attributes["target_temp_low"]), _heat_number(attributes["target_temp_high"])
            result["targetLow"], result["targetHigh"] = (None, None) if low is not None and high is not None and low > high else (low, high)
    return result


def _entity(e: Entity, control: Collection[str]) -> dict | None:
    domain = e.entity_id.split(".", 1)[0]
    device = {**_device(e.device, e.maker, e.device_id, e.model), **({"entityCategory": "config"} if e.category == "config" else {})}
    if domain in SETTING_DOMAINS:  # HA-021: a select or number of a whole device; one the server would refuse (no options, no range) is not sent
        shown = setting(domain, e.state, e.attributes)
        if domain == "select" and not shown["options"] or domain == "number" and (shown["min"] is None or shown["step"] is None):
            return None
        item = {
            "entityId": e.entity_id,
            "domain": domain,
            "name": _name(e.name, e.entity_id),
            "state": e.state if _option_text(e.state) else "unknown",
            "brightness": None,
            "supportsBrightness": False,
            "lastChanged": _iso(e.last_changed) if e.last_changed else None,
            "kind": domain,
            **({"options": shown["options"]} if domain == "select" else {
                "value": shown["value"], "min": shown["min"], "max": shown["max"], "step": shown["step"],
                "unit": e.unit if isinstance(e.unit, str) and _utf16(e.unit) <= MAX_UNIT else None}),
            **device,
        }
        if control:
            item["controllable"] = e.entity_id in control
        return item
    if domain in HEAT_DOMAINS:  # HA-013: read only like a binary sensor; lastChanged is the reading's own time (last_reported), see Pusher.entities
        state = e.state if _heat_text(e.state) else "unknown"
        item = {
            "entityId": e.entity_id,
            "domain": domain,
            "name": _name(e.name, e.entity_id),
            "state": state,
            "brightness": None,
            "supportsBrightness": False,
            "lastChanged": _iso(e.last_changed) if e.last_changed else None,
            "kind": domain,
            "heat": heat(domain, state, e.attributes, e.unit),
            **device,
        }
        if control:  # HA-014: the component decides it (the control tick and TARGET_TEMPERATURE), see controllable()
            item["controllable"] = e.entity_id in control
        return item
    if domain in ("binary_sensor", "person"):  # HA-012: read only; a person has no timing (a zone move would leak it) and no attribute
        person = domain == "person"
        item = {
            "entityId": e.entity_id,
            "domain": domain,
            "name": _name(e.name, e.entity_id),
            "state": _person_state(e.state) if person else e.state if e.state in STATES else "unknown",
            "brightness": None,
            "supportsBrightness": False,
            "lastChanged": None if person or not e.last_changed else _iso(e.last_changed),
            "kind": "person" if person else "binary",
        }
        if not person:
            item["deviceClass"] = e.device_class
            item.update(device)
        if control:
            item["controllable"] = False
        return item
    supports = domain == "light" and e.supports_brightness
    state = e.state if e.state in STATES else "unknown"
    level = e.brightness if supports and state == "on" and isinstance(e.brightness, (int, float)) else None
    item = {
        "entityId": e.entity_id,
        "domain": domain,
        "name": _name(e.name, e.entity_id),
        "state": state,
        "brightness": None if level is None else min(100, max(0, round(level * 100 / 255))),
        "supportsBrightness": supports,
        "lastChanged": _iso(e.last_changed) if e.last_changed else None,
        **device,
    }
    if control:  # expand -> contract: the 8th key only while there is a control list (an HA-002 backend takes 7)
        item["controllable"] = e.entity_id in control
    return item


class Sensor(NamedTuple):
    """A picked temperature, power or energy sensor, as read from the HA state machine (HA-005)."""

    entity_id: str
    name: str | None
    device_class: str | None
    state: str
    unit: str | None
    last_reported: datetime
    device: str | None = None  # HA-016
    maker: str | None = None
    device_id: str | None = None  # HA-021: only for a 0.20.0-shape post
    model: str | None = None
    state_class: str | None = None  # HA-021: measurement, total or total_increasing
    energy_role: str | None = None  # HA-021 (D6): grid, solar or battery when the Energy dashboard uses the sensor


def _utf16(text: str) -> int:
    """A length as the server counts it (UTF-16 code units), so a non-BMP character cannot pass here and fail there."""
    return len(text.encode("utf-16-le")) // 2


def _sensors(sensors: Sequence[Sensor], v2: bool = False, cap: int = MAX_SENSORS) -> list[dict]:
    """The sensor items the server accepts: valid ids once, the three classes, bounded state and unit, at most 32.
    HA-021: the 0.20.0 shape takes any class of [a-z0-9_]{1,32} or none, the state class and the Energy dashboard role, and up to cap."""
    items, seen = [], set()
    for s in sensors:
        if not SENSOR_ID.fullmatch(s.entity_id) or s.entity_id in seen \
                or not (s.device_class is None or ANY_CLASS.fullmatch(s.device_class) if v2 else s.device_class in SENSOR_CLASSES):
            continue
        seen.add(s.entity_id)
        state = str(s.state)
        item = {
            "entityId": s.entity_id,
            "name": _name(s.name, s.entity_id),
            "deviceClass": s.device_class,
            "state": state if _utf16(state) <= MAX_STATE else "unknown",
            "unit": s.unit if isinstance(s.unit, str) and _utf16(s.unit) <= MAX_UNIT else None,
            "lastReported": _iso(s.last_reported),
            **_device(s.device, s.maker, s.device_id, s.model),
        }
        if s.state_class in STATE_CLASSES:
            item["stateClass"] = s.state_class
        if s.energy_role in ENERGY_ROLES:
            item["energyRole"] = s.energy_role
        items.append(item)
    return items[:cap]


def settings(integration: str, states: Mapping[str, str]) -> dict | None:
    """ISET-004: the "settings" object of the hub's integration: the states of its profile's entities, without unavailable or
    unknown ones (a stalled hub sends none) and within the server's state bound; None without a profile or any such state."""
    wanted = SETTINGS_ENTITIES.get(integration, frozenset())
    sent = [(key, str(state)) for key, state in sorted(states.items())
            if key in wanted and str(state) not in ("unavailable", "unknown") and _utf16(str(state)) <= MAX_STATE]
    return {"integration": integration, "states": dict(sent[:MAX_SETTINGS])} if sent else None


_PLUGIN = re.compile(r"[A-Za-z0-9_]{1,32}")  # the server's plugin rule


def plugin_name(plugin: object) -> str | None:
    """The hub entry's solax_modbus plugin (an old-style plugin path cut to its name, as solax_modbus does), None outside the server's rule."""
    if isinstance(plugin, str) and "plugin_" in plugin:
        plugin = plugin.split("plugin_", 1)[1].removesuffix(".py")
    return plugin if isinstance(plugin, str) and _PLUGIN.fullmatch(plugin) else None


def hub(plugin: object, manufacturer: object, family: object = None) -> dict:
    """HA-008 (0.7.3): the solax envelope's "plugin" (see plugin_name) and "manufacturer" (its inverter device's). solax_modbus also
    runs Growatt, Solis, Sofar ... hubs under its one domain; SunnyHome decides which it reads. Each is None when unknown or outside
    the server's rules. SMB-004 (0.8.0): a Growatt hub adds "family", its inverter device's serial number (the plugin's detection
    identifier, a serial or a firmware string), which SunnyHome matches against the hybrid families."""
    name = plugin_name(plugin)
    info = {
        "plugin": name,
        "manufacturer": manufacturer if isinstance(manufacturer, str) and 0 < _utf16(manufacturer) <= MAX_NAME and manufacturer.isprintable() else None,
    }
    if name == "growatt":
        info["family"] = ident(family)
    return info


def build(
    sent_at: datetime,
    serial: str | None,
    readings: list[tuple[str, str | None, str, datetime]] | None,
    entities: Sequence[Entity] = (),
    control: Collection[str] = (),
    sensors: Sequence[Sensor] | None = None,
    remote: dict | None = None,
    hub_settings: dict | None = None,
    integration: str = "solax_modbus",
    hub_info: dict | None = None,
    wallbox: dict | None = None,
    wallboxes: list[dict] | None = None,
    v2: bool = False,
    limits: tuple[int, int] = (MAX_ENTITIES, MAX_SENSORS),
) -> dict:
    """Body of POST /api/ha/v1/states.

    readings are (key, unit, state, last_reported) tuples of the single inverter integration entry,
    or None when there is not exactly one, which sends "solax": null.
    integration is that entry's domain: solax_modbus posts "solax"; GW-002: goodwe posts "inverter" {integration, serial,
    readings} in place of "solax" (no remote or settings: SunnyHome reads no GoodWe setting and controls no GoodWe inverter).
    control is the entry's control list (entity ids SunnyHome may command).
    sensors is None while no sensor is ticked in the options, which sends no "sensors" key (HA-005).
    remote is the inverter's remote state (BAT-008), given whenever the hub has the mode sensor (BAT-003C).
    hub_settings is the hub's settings object (ISET-004, see settings()), sent beside the readings.
    hub_info is the hub's plugin and manufacturer (HA-008, see hub()), sent beside the readings of a solax_modbus hub.
    wallbox is the EV-charger hub's envelope (EV-004, see wallbox_report()), sent beside either inverter key.
    wallboxes is the list of the Other wallbox item (WALLBOX-005, see generic_report()), sent beside "wallbox".
    HA-021: v2 is the 0.20.0 shape (sensors of any class); limits are the entity and sensor caps (64/32, or 512/512 for a gzip post). The caller passes
    the new keys and kinds only for that shape.
    """
    solax = None if readings is None else {
        "serial": serial,
        "readings": [
            {"key": key, "unit": unit, "state": str(state), "lastReported": _iso(reported)}
            for key, unit, state, reported in readings[:MAX_READINGS]
        ],
    }
    items = [item for e in entities if _STATE_ID.fullmatch(e.entity_id) and (e.device_class in BINARY_CLASSES or not e.entity_id.startswith("binary_sensor."))
             and (item := _entity(e, control)) is not None][:limits[0]]
    if solax is not None and integration != "solax_modbus":
        body = {"sentAt": _iso(sent_at), "inverter": {"integration": integration, **solax}, "entities": items}
    else:
        if solax is not None and remote is not None:
            solax["remote"] = remote
        if solax is not None and hub_settings is not None:
            solax["settings"] = hub_settings
        if solax is not None and hub_info is not None:
            solax.update(hub_info)
        body = {"sentAt": _iso(sent_at), "solax": solax, "entities": items}
    if sensors is not None:  # expand -> contract: only while sensors are ticked (a backend before HA-005 refuses the key)
        body["sensors"] = _sensors(sensors, v2, limits[1])
    if wallbox is not None:  # EV-004: only with an EV-charger hub (a backend before EV-004 refuses a solax_modbus wallbox)
        body["wallbox"] = wallbox
    if wallboxes is not None:  # WALLBOX-005: only with an Other wallbox set up (a backend before it refuses the key)
        body["wallboxes"] = wallboxes
    return body


def encode(body: dict, order: Sequence[str] | None = None, gzip_budget: int = GZIP_BUDGET, json_budget: int = JSON_BUDGET) -> tuple[bytes, frozenset]:
    """HA-021: the gzip bytes of a 0.20.0-shape post (UTF-8 JSON, not escaped: aiohttp's json= would make each non-ASCII character 6 bytes) and
    the entity ids trimmed: over a budget, the items last in order (the priority, most important first: the explicitly ticked ones, then the whole
    devices' entities sent before, then new ones; by default the entity ids) go first, a tenth at a time, so the server never refuses the post for its
    size."""
    dropped: frozenset = frozenset()
    while True:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        data = gzip.compress(raw, compresslevel=6, mtime=0)
        present = {item["entityId"] for key in ("entities", "sensors") for item in body.get(key) or ()}
        rank = {eid: n for n, eid in enumerate(order if order is not None else sorted(present))}
        ids = sorted(present, key=lambda eid: rank.get(eid, len(rank)))  # an id outside the order goes first
        if len(raw) <= json_budget and len(data) <= gzip_budget or not ids:
            return data, dropped
        drop = frozenset(ids[-max(1, len(ids) // 10):])
        body = {**body, **{key: [item for item in body[key] if item["entityId"] not in drop] for key in ("entities", "sensors") if key in body}}
        dropped |= drop


# Commands (HA-003): POST /api/ha/v1/commands/claim and /commands/result. AUTO-004 (0.3.0): a claim with the X-SunnyHome-Leases
# header gets a sixth key, leaseS: the lease length of an automation on/level or a lease_renew; lease_cancel ends a lease.

TTL_MAX_MS = 30_000  # HA runs no command later than 30 s after sending the claim, whatever ttlMs the server sends
BUSY_S = 30  # a later command is refused while the entity's previous service call runs longer than this
WATCH_S = 10  # how long the target state is awaited after the call was issued
POLL_S = 0.2
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COMMAND_KEYS = frozenset({"id", "entityId", "action", "level", "ttlMs"})
_LEASED_KEYS = _COMMAND_KEYS | {"leaseS"}
# HA-014: a command on a heat entity, whatever its action, has this shape; value and mode are null where the action has none.
_HEAT_KEYS = _COMMAND_KEYS | {"value", "mode"}
_HEAT_LEASED_KEYS = _HEAT_KEYS | {"leaseS"}
# HA-021 (0.20.0): selects, numbers, presets and range targets take the heat shape too; a 0.20.0 claimer may also get "valueHigh" (a range's high
# target, else null), with or without a lease. Both shapes are accepted, whichever the server sends.
_HEAT_SHAPES = (_HEAT_KEYS, _HEAT_LEASED_KEYS, _HEAT_KEYS | {"valueHigh"}, _HEAT_LEASED_KEYS | {"valueHigh"})
HEAT_ACTIONS = ("set_temperature", "set_mode")
SETTING_ACTIONS = {"select": "select_option", "number": "set_value"}  # HA-021: the one action of each kind (the option rides in mode, the number in value)
# HA-021 (§2g): what a restore lease holds, per key: the field of Seen.heat it compares (a select's and a number's Seen.heat is setting()).
_FIELDS = {"temperature": "target", "mode": "mode", "preset": "presetMode", "low": "targetLow", "high": "targetHigh", "option": "option", "value": "value"}
_TEXT_KEYS = frozenset({"mode", "preset", "option"})
_HEAT_SPEC = frozenset({"temperature", "mode"})  # a climate's or water heater's lease always has these two (the 0.19.0 store shape); the rest only when set
_BACK_REQUIRED = frozenset({"option", "value", "preset", "low", "high"})  # HA-021 (§2g): a leased grant of these needs a previous value to put back
# HA-017: a climate's fan / vane commands: the value rides in mode (the _HEAT_KEYS shape), never leased; action -> (value key, list key, service field).
AIR_ACTIONS = {"set_fan_mode": ("fanMode", "fanModes", "fan_mode"),
               "set_swing_mode": ("swingMode", "swingModes", "swing_mode"),
               "set_swing_horizontal_mode": ("swingHorizontalMode", "swingHorizontalModes", "swing_horizontal_mode")}
HEAT_RESTORE_GIVE_UP_S = 3600  # a restore not confirmed within an hour of the lease end is given up (a try puts back only what still shows the lease's value)
_UNAVAILABLE = ("unavailable", "unknown")
LEASE_MIN_S, LEASE_MAX_S = 60, 3600
_LOGGER = logging.getLogger(__name__)


class Command(NamedTuple):
    id: str
    entity_id: str
    action: str  # on, off, level; AUTO-004: lease_renew, lease_cancel
    level: int | None  # 1-100 for level, else None
    ttl_ms: int
    lease_s: int | None = None  # AUTO-004: the lease of an automation on/level, or the renewal's length
    value: float | None = None  # HA-014: set_temperature's target (a restore: the previous target to put back); HA-021: a range's low, set_value's number
    mode: str | None = None  # HA-014: set_mode's mode, or set_temperature's optional mode (a restore: the previous mode); HA-021: the option or preset
    value_high: float | None = None  # HA-021: a range set_temperature's high target
    back: dict | None = None  # HA-021: a restore's values per lease key (heat_verdict), which service_calls and reached read


class Seen(NamedTuple):
    """An entity's current state, as the executor needs it."""

    state: str
    brightness: int | float | None  # HA scale 0-255
    dimmable: bool
    assumed: bool
    heat: dict | None = None  # HA-014: heat() of a climate or water heater (target, mode, min, max, step, modes), as the component reports it


class Env(NamedTuple):
    """What the executor needs from Home Assistant; the tests pass fakes."""

    locks: dict  # entity id -> asyncio.Lock, shared by all entries
    inflight: dict  # entity id -> (service call task, start); shared by all entries and kept across reloads
    allowed: Callable[[str], bool]  # in this entry's control list
    state: Callable[[str], Seen | None]
    dispatch: Callable[[str, str, dict], asyncio.Future]  # raises only when the service did not start
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable] = asyncio.sleep
    leases: Leases | None = None  # AUTO-004: the entry's leases (None: an executor without leases)
    dispatch_many: Callable[[list], asyncio.Future] | None = None  # HA-014: several service calls in one task and one context (None: chained over dispatch)
    save: Callable[[], Awaitable] | None = None  # persists the leases; awaited before a leased service call
    wall: Callable[[], float] = time.time  # the lease clock (UTC seconds), persisted across restarts


def is_heat(entity_id: object) -> bool:
    return isinstance(entity_id, str) and _HEAT_ENTITY.fullmatch(entity_id) is not None


def is_setting(entity_id: object) -> bool:
    """HA-021: a select or number entity (commanded and leased in the heat shape)."""
    return isinstance(entity_id, str) and _SETTING_ENTITY.fullmatch(entity_id) is not None


def restorable(entity_id: object) -> bool:
    """HA-021: an entity whose rule changes are held by a restore lease (heat, select, number); a switch or light keeps the on/off lease."""
    return is_heat(entity_id) or is_setting(entity_id)


def parse_command(item: object) -> tuple[str | None, Command | None]:
    """Strict check of one claimed command: (id, command); command None if invalid, id None if the id is."""
    if not isinstance(item, dict) or not isinstance(cid := item.get("id"), str) or not _UUID.fullmatch(cid):
        return None, None
    entity_id, action, level, ttl, lease = (item.get(key) for key in ("entityId", "action", "level", "ttlMs", "leaseS"))
    leasing = type(lease) is int and LEASE_MIN_S <= lease <= LEASE_MAX_S
    if restorable(entity_id):  # HA-014: set_temperature (a lease and a mode are optional), set_mode and the lease renewal and cancel; never on, off or level
        value, mode, high = item.get("value"), item.get("mode"), item.get("valueHigh")
        climate = entity_id.startswith("climate.")
        own = SETTING_ACTIONS.get(entity_id.split(".", 1)[0])  # HA-021: a select's select_option, a number's set_value; None for a heat entity
        low_n, high_n = _heat_number(value), _heat_number(high)
        valid = (
            item.keys() in _HEAT_SHAPES and level is None and type(ttl) is int and ttl >= 0
            and (high is None or action == "set_temperature" and climate and low_n is not None and high_n is not None and low_n <= high_n)  # HA-021 (D7): a range
            and (own is None and action == "set_temperature" and low_n is not None and (mode is None or _heat_text(mode) is not None) and (lease is None or leasing)
                 or own is None and action == "set_mode" and value is None and _heat_text(mode) is not None and lease is None
                 or action in AIR_ACTIONS and climate and value is None and _heat_text(mode) is not None and lease is None
                 # HA-021: a preset (climate), an option (select) or a number (number), each with an optional lease
                 or action == "set_preset_mode" and climate and value is None and _heat_text(mode) is not None and (lease is None or leasing)
                 or action == "select_option" == own and value is None and _option_text(mode) is not None and (lease is None or leasing)
                 or action == "set_value" == own and _number(value) is not None and mode is None and (lease is None or leasing)
                 or action == "lease_renew" and value is None and mode is None and leasing
                 or action == "lease_cancel" and value is None and mode is None and lease is None)
        )
        value = low_n if action == "set_temperature" else _number(value) if action == "set_value" else None
        return cid, (Command(cid, entity_id, action, None, ttl, lease, value, mode, high_n) if valid else None)
    valid = (
        item.keys() in (_COMMAND_KEYS, _LEASED_KEYS)
        and isinstance(entity_id, str) and _ENTITY_ID.fullmatch(entity_id) is not None
        # 1-100 only: brightness_pct 0 would switch the light off
        and (level is None if action in ("on", "off", "lease_renew", "lease_cancel")
             else action == "level" and type(level) is int and 1 <= level <= 100)
        and type(ttl) is int and ttl >= 0
        # AUTO-004: a renewal has a length, a cancel and an off none; an on or level may carry one
        and (leasing if action == "lease_renew" else lease is None if action in ("off", "lease_cancel") else lease is None or leasing)
    )
    return cid, (Command(cid, entity_id, action, level, ttl, lease) if valid else None)


def deadline(sent: float, ttl_ms: int) -> float:
    return sent + min(ttl_ms, TTL_MAX_MS) / 1000


def service_call(cmd: Command) -> tuple[str, str, dict]:
    domain = cmd.entity_id.split(".", 1)[0]
    data = {"entity_id": cmd.entity_id}
    if cmd.action == "level":
        data["brightness_pct"] = cmd.level
    return domain, "turn_off" if cmd.action == "off" else "turn_on", data


def _near(shown: object, want: float, step: object) -> bool:
    """A target within what the entity can show: half a step (integrations round to their precision), at least 0.05."""
    return isinstance(shown, (int, float)) and abs(shown - want) <= max((step if isinstance(step, (int, float)) and step > 0 else 0.5) / 2, 0.05)


def _shows(key: str, want: object, heat: dict) -> bool:
    shown = heat.get(_FIELDS[key])
    if key == "preset" and want == "none" and shown is None:
        return True  # HA-021 (§2g): no preset active, as many climates report it
    if key == "value":  # HA-021: a number shows exactly what was set (a previous value may sit off the step grid, so half a step is too loose)
        return isinstance(shown, (int, float)) and abs(shown - want) <= 1e-6 * max(1.0, abs(want))
    return shown == want if key in _TEXT_KEYS else _near(shown, want, heat.get("step"))


def heat_shows(spec: dict | None, seen: Seen | None) -> bool:
    """HA-014: whether the entity shows every value the spec holds ({"temperature", "mode"}; a None key is not held). HA-021: also preset, low, high,
    option and value (_FIELDS)."""
    if seen is None or seen.heat is None or spec is None:
        return False
    return all(_shows(key, want, seen.heat) for key, want in spec.items() if want is not None)


def _final_mode(cmd: Command, seen: Seen | None) -> str | None:
    """The hvac (operation) mode the entity ends in; HA-017: a fan / vane command's mode is its value, so it leaves the mode as shown (HA-021: so does
    a preset's or an option's)."""
    own = cmd.mode if cmd.action in ("set_temperature", "set_mode", "restore") else None
    return own if own is not None else seen.heat.get("mode") if seen is not None and seen.heat is not None else None


def _sets(cmd: Command, seen: Seen | None) -> dict:
    """HA-021: the values a command leaves on the entity, per lease key (_FIELDS); None where it sets nothing. A target (single or range) is not set
    when the resulting mode is off (service_calls sends none then)."""
    if cmd.action in SETTING_ACTIONS.values():
        return {"option": cmd.mode} if cmd.action == "select_option" else {"value": cmd.value}
    if cmd.action == "set_preset_mode":
        return {"preset": cmd.mode}
    off = _final_mode(cmd, seen) == "off"
    if cmd.action == "restore":
        back = dict(cmd.back) if cmd.back is not None else {"temperature": cmd.value, "mode": cmd.mode}
        return {key: None if off and key in ("temperature", "low", "high") else want for key, want in back.items()}
    if cmd.value_high is not None:
        return {"mode": cmd.mode, "low": None if off else cmd.value, "high": None if off else cmd.value_high}
    return {"temperature": None if off else cmd.value, "mode": cmd.mode}


def service_calls(cmd: Command, seen: Seen | None) -> list[tuple[str, str, dict]]:
    """The service calls of one command: one for a switch or light; for a heat entity the mode first (not when it is already shown), then the
    temperature (not when the resulting mode is off: many integrations refuse a setpoint then). An empty list: nothing to call.
    HA-021: select_option and set_value one call each; set_preset_mode one call; a range target is one set_temperature with both ends; a restore
    puts its values back in the same order (mode, preset, then the target or the range)."""
    if not restorable(cmd.entity_id):
        return [service_call(cmd)]
    domain = cmd.entity_id.split(".", 1)[0]
    data = {"entity_id": cmd.entity_id}
    if cmd.action in AIR_ACTIONS:  # HA-017: one call, never the hvac mode or the temperature
        return [(domain, cmd.action, {**data, AIR_ACTIONS[cmd.action][2]: cmd.mode})]
    wanted = _sets(cmd, seen)
    if domain in SETTING_DOMAINS:
        key = "option" if domain == "select" else "value"
        return [] if wanted.get(key) is None else [(domain, SETTING_ACTIONS[domain], {**data, key: wanted[key]})]
    calls = []
    mode = wanted.get("mode")
    if mode is not None and mode != (seen.heat.get("mode") if seen is not None and seen.heat is not None else None):
        calls.append((domain, "set_hvac_mode" if domain == "climate" else "set_operation_mode",
                      {**data, "hvac_mode" if domain == "climate" else "operation_mode": mode}))
    if wanted.get("preset") is not None:
        calls.append((domain, "set_preset_mode", {**data, "preset_mode": wanted["preset"]}))
    target = {field: wanted[key] for key, field in (("temperature", "temperature"), ("low", "target_temp_low"), ("high", "target_temp_high"))
              if wanted.get(key) is not None}
    if target:
        calls.append((domain, "set_temperature", {**data, **target}))
    return calls


def _chain(dispatch: Callable, calls: list) -> asyncio.Future:
    """Several calls in one task over a plain dispatch: the first starts at once (it may raise), the rest follow only if it did not."""
    first = dispatch(*calls[0])

    async def rest() -> None:
        await first
        for call in calls[1:]:
            await dispatch(*call)
    return asyncio.ensure_future(rest())


def in_range(value: float, heat: dict, from_min: bool = False) -> bool:
    """HA-014: within the entity's own min and max (both known) and on its step grid (value / step whole, or exactly min or max). HA-021: a number's
    grid counts from its min (from_min, as the server's onGrid does for set_value), a heat target's from 0."""
    low, high, step = heat.get("min"), heat.get("max"), heat.get("step")
    if low is None or high is None or not isinstance(step, (int, float)) or step <= 0:
        return False
    cells = (value - low if from_min else value) / step
    return low - 1e-9 <= value <= high + 1e-9 and (abs(cells - round(cells)) <= 1e-6 or abs(value - low) <= 1e-9 or abs(value - high) <= 1e-9)


def reached(cmd: Command, seen: Seen | None) -> bool:
    """The target state: on or off; for a level on with the brightness within 1 %; HA-014: the target and mode of a heat command (HA-021: and what a
    select, number, preset, range or restore command sets)."""
    if seen is None:
        return False
    if cmd.action in AIR_ACTIONS:  # HA-017
        return seen.heat is not None and seen.heat.get(AIR_ACTIONS[cmd.action][0]) == cmd.mode
    if restorable(cmd.entity_id):
        return heat_shows(_sets(cmd, seen), seen)
    if cmd.action != "level":
        return seen.state == cmd.action
    b = seen.brightness
    return seen.state == "on" and isinstance(b, (int, float)) and abs(round(b * 100 / 255) - cmd.level) <= 1


async def _wait_inflight(eid: str, env: Env) -> tuple[str, str | None, bool] | None:
    """A command waits for the entity's previous service call (never cancelled); None when it ended, else the refusal."""
    if prior := env.inflight.get(eid):
        task, started = prior
        while not task.done():
            if env.clock() >= started + BUSY_S:
                return "failed", "entity_busy", False
            await env.sleep(POLL_S)
    return None


async def execute(cmd: Command, until: float, env: Env) -> tuple[str, str | None, bool]:
    """Run one command; (outcome, reason, assumedState) of the result.

    Single writer per entity across entries and reloads: the per-entity lock keeps claim order, and a
    command waits for the entity's previous service call (never cancelled) before its own checks.
    "failed" only when nothing was dispatched; after dispatch only "done" (target observed) or "unknown".
    AUTO-004: a leased on/level records its lease (saved) before its checks and drops it again when refused;
    every other command for the entity ends the entity's lease; lease_renew and lease_cancel call no service.
    HA-014: a leased set_temperature records a restore lease (the previous target and mode, read first, and the values it sets).
    HA-021 (§2g): so does a leased select_option, set_value, set_preset_mode or range set_temperature (per key: option, value, preset, low and high).
    """
    eid = cmd.entity_id
    async with env.locks.setdefault(eid, asyncio.Lock()):
        if busy := await _wait_inflight(eid, env):
            return busy
        if cmd.action in ("lease_renew", "lease_cancel"):
            return await _lease_command(cmd, until, env)
        leased = cmd.lease_s is not None and env.leases is not None
        before = env.leases.items.get(eid) if env.leases is not None else None
        if env.leases is not None:
            if leased:
                if restorable(eid):
                    # The previous values are read before anything is written; an entity that shows none gets no lease (and is refused below).
                    if (seen := env.state(eid)) is not None and seen.heat is not None and seen.state not in _UNAVAILABLE:
                        env.leases.grant_restore(eid, cmd, seen, env.wall())
                        await env.save()
                else:
                    env.leases.grant(eid, cmd.id, cmd.lease_s, cmd.action, cmd.level, env.wall())
                    await env.save()  # saved before the call: a crash right after it never leaves an on without its lease
            elif env.leases.cancel(eid) is not None:
                await env.save()
        # No await from here to the dispatch: the checks hold when the call starts.
        refusal = _refusal(cmd, until, env)
        if refusal is None and leased and restorable(eid) and (lease := env.leases.items.get(eid)) is not None and lease.command == cmd.id \
                and any(lease.prev.get(key) is None for key, want in lease.set.items() if want is not None and key in _BACK_REQUIRED):
            refusal = "restore_invalid"  # HA-021 (§2g): nothing to put back at the lease end, so the rule's value would stay
        task = None
        if refusal is None:
            try:
                calls = service_calls(cmd, env.state(eid))
                if len(calls) == 1:
                    task = env.dispatch(*calls[0])
                elif calls:
                    task = (env.dispatch_many or partial(_chain, env.dispatch))(calls)
            except Exception as err:  # raised before the service ran, so nothing was switched
                _LOGGER.debug("Service call refused: %s", type(err).__name__)
                refusal = "service_error"
        if refusal is not None:
            if leased:
                if before is not None and restorable(eid):
                    env.leases.items[eid] = before  # the earlier restore lease still holds the real previous values
                else:
                    env.leases.cancel(eid)
                await env.save()
            return "failed", refusal, False
        return await _watch(cmd, task, env)


async def _watch(cmd: Command, task: asyncio.Future | None, env: Env) -> tuple[str, str | None, bool]:
    """After the dispatch: "done" once the target shows (within WATCH_S), else "unknown"; task None: nothing was to be called."""
    eid = cmd.entity_id
    if task is not None:
        env.inflight[eid] = (task, env.clock())
        task.add_done_callback(partial(_finished, env.inflight, eid))
    end = env.clock() + WATCH_S
    while task is None or not (task.done() and (task.cancelled() or task.exception() is not None)):
        if reached(cmd, seen := env.state(eid)):
            return "done", "assumed_state" if seen.assumed else None, seen.assumed
        if env.clock() >= end:
            return "unknown", "no_confirmation", False
        await env.sleep(POLL_S)
    return "unknown", "service_error", False  # the call failed after it started (e.g. ServiceValidationError)


def _actions(eid: str) -> tuple[str, ...]:
    """The actions an entity takes (a restore is the lease end's own command)."""
    if is_heat(eid):
        return (*HEAT_ACTIONS, *AIR_ACTIONS, "restore") + (("set_preset_mode",) if eid.startswith("climate.") else ())
    if is_setting(eid):
        return SETTING_ACTIONS[eid.split(".", 1)[0]], "restore"
    return "on", "off", "level"


def _refusal(cmd: Command, until: float, env: Env) -> str | None:
    """Why a command is refused before any service call, or None."""
    eid = cmd.entity_id
    if env.clock() >= until:
        return "expired_on_arrival"
    if not env.allowed(eid):
        return "not_controllable"
    seen = env.state(eid)
    if seen is None or seen.state in _UNAVAILABLE:
        return "entity_unavailable"
    if cmd.action not in _actions(eid):
        return "invalid_command"  # nothing but a target or a mode is ever sent to a heat entity (a climate has its own turn_on/turn_off); HA-017: or a fan / vane value
    if restorable(eid):
        if seen.heat is None:
            return "entity_unavailable"
        if cmd.action in AIR_ACTIONS:  # HA-017: only a value of the list the entity reports now
            return None if cmd.mode in seen.heat.get(AIR_ACTIONS[cmd.action][1], ()) else "mode_unsupported"
        if cmd.action == "restore":
            return None
        if cmd.action in ("select_option", "set_preset_mode"):  # HA-021: only an option or preset the entity lists now (the text, never an index)
            return None if cmd.mode in seen.heat.get("options" if cmd.action == "select_option" else "presetModes", ()) else "mode_unsupported"
        if cmd.action == "set_value":  # HA-021: within the number's own min and max, on its step grid
            return None if in_range(cmd.value, seen.heat, from_min=True) else "out_of_range"
        if cmd.action == "set_temperature" and cmd.value_high is not None:  # HA-021 (D7): a range climate, both ends in range and low <= high
            if "targetLow" not in seen.heat or not (in_range(cmd.value, seen.heat) and in_range(cmd.value_high, seen.heat)) or cmd.value > cmd.value_high:
                return "out_of_range"
        elif cmd.action == "set_temperature" and (not in_range(cmd.value, seen.heat) or seen.heat.get("target") is None and seen.heat.get("targetLow") is not None):
            return "out_of_range"  # HA-021: a climate that shows a range takes a range, never a single target
        if cmd.mode is not None and cmd.mode not in seen.heat.get("modes", ()):
            return "mode_unsupported"
        if cmd.action == "set_temperature" and cmd.mode is None and _final_mode(cmd, seen) == "off":
            return "mode_off"  # HA-014: no setpoint is sent in mode off (service_calls), so it would only look done; an explicit mode (even off) is the caller's choice
        return None
    if cmd.action == "level" and not (eid.startswith("light.") and seen.dimmable):
        return "not_dimmable"
    return None


async def _lease_command(cmd: Command, until: float, env: Env) -> tuple[str, str | None, bool]:
    """AUTO-004: a renewal extends only an active lease whose entity is still at its target; a cancel ends the lease (done also when none).
    HA-014: a cancel of a restore lease puts the previous target and mode back first (the rule's condition ended); a restore that does not
    complete leaves the lease due, so the lease check retries it."""
    if cmd.action == "lease_cancel":
        lease = env.leases.items.get(cmd.entity_id) if env.leases is not None else None
        if lease is not None and lease.target == "restore":
            env.leases.items[cmd.entity_id] = lease._replace(until=min(lease.until, env.wall()))
            await env.save()
            outcome = await _restore_locked(cmd.entity_id, env.leases.items[cmd.entity_id], env)
            if outcome[0] == "done" or outcome[1] in ("outside_change", "not_controllable", "restore_invalid"):
                env.leases.cancel(cmd.entity_id)
                await env.save()
            if outcome[1] in ("outside_change", "not_controllable", "restore_invalid"):  # HA-021 (§2g): done, but nothing was put back, and why (AUTO-028 post review: not_controllable too)
                return "done", outcome[1], False
        elif env.leases is not None and env.leases.cancel(cmd.entity_id) is not None:
            await env.save()
        return "done", None, False
    if env.clock() >= until:
        return "failed", "expired_on_arrival", False
    if not env.allowed(cmd.entity_id):
        return "failed", "not_controllable", False
    if env.leases is None:
        return "failed", "lease_cancelled", False
    reason = env.leases.renew(cmd.entity_id, cmd.id, cmd.lease_s, env.state(cmd.entity_id), env.wall())
    await env.save()
    return ("failed", reason, False) if reason else ("done", None, False)


def heat_verdict(lease: "Lease", seen: Seen | None) -> tuple[str, Command | None]:
    """HA-014: what a restore lease's end does, per key the lease changed. A key still at its set value is put back (to prev); at prev it
    is done; at anything else someone changed it by hand. ("restore", cmd) | ("ended", None) | ("cancelled", None) (a key was changed by hand)."""
    heat = seen.heat if seen is not None else None
    if heat is None:
        return "cancelled", None
    back = {}
    for key, want in lease.set.items():
        if want is None:
            continue
        old = lease.prev.get(key)
        if old is None:  # nothing to put back for this key
            if not _shows(key, want, heat):
                return "cancelled", None
        elif _shows(key, old, heat):
            continue
        elif _shows(key, want, heat):
            back[key] = old
        else:
            return "cancelled", None
    if not back:
        return "ended", None
    if "low" in back or "high" in back:  # HA-021: a range goes back in one call with both ends
        back["low"], back["high"] = lease.prev.get("low"), lease.prev.get("high")
    return "restore", Command(lease.command, "", "restore", None, 0, None, back.get("temperature"), back.get("mode"), None, back)


def _within(value: float, heat: dict) -> bool:
    """HA-021 (§2g): within the entity's own min and max (both known), the only check HA itself makes for set_value and set_temperature: a value
    HA showed (23 with step 5, 21.1 from °F) goes back even off the step grid."""
    low, high = heat.get("min"), heat.get("max")
    return low is not None and high is not None and low - 1e-9 <= value <= high + 1e-9


def back_valid(back: dict, heat: dict) -> bool:
    """HA-021 (§2g): whether the values a restore puts back still validate against the live state (option still listed, value in min..max, preset
    listed, a range in min..max with low <= high); a restore that does not is never written (restore_invalid). HA-014's target and mode stay unchecked."""
    low, high = back.get("low"), back.get("high")
    return ((back.get("option") is None or back["option"] in heat.get("options", ()))
            and (back.get("value") is None or _within(back["value"], heat))
            and (back.get("preset") is None or back["preset"] in heat.get("presetModes", ()))
            and (low is None and high is None or low is not None and high is not None and _within(low, heat) and _within(high, heat) and low <= high))


async def _restore_locked(eid: str, lease: "Lease", env: Env) -> tuple[str, str | None, bool]:
    """The entity lock held: put a restore lease's previous values back where the entity still shows the lease's values."""
    if not env.allowed(eid):
        return "failed", "not_controllable", False
    seen = env.state(eid)
    if seen is None or seen.state in _UNAVAILABLE or seen.heat is None:
        return "failed", "entity_unavailable", False
    verdict, cmd = heat_verdict(lease, seen)
    if verdict == "cancelled":
        return "failed", "outside_change", False
    if cmd is None:
        return "done", None, False
    if not back_valid(cmd.back, seen.heat):
        return "failed", "restore_invalid", False
    cmd = cmd._replace(entity_id=eid)
    calls = service_calls(cmd, seen)
    task = None
    try:
        if len(calls) == 1:
            task = env.dispatch(*calls[0])
        elif calls:
            task = (env.dispatch_many or partial(_chain, env.dispatch))(calls)
    except Exception as err:  # raised before the service ran
        _LOGGER.debug("Service call refused: %s", type(err).__name__)
        return "failed", "service_error", False
    return await _watch(cmd, task, env)


async def restore(eid: str, lease: "Lease", env: Env) -> tuple[str, str | None, bool]:
    """HA-014: the lease check's restore of a due restore lease; ("done", ...) also when nothing was left to put back."""
    async with env.locks.setdefault(eid, asyncio.Lock()):
        if busy := await _wait_inflight(eid, env):
            return busy
        if env.leases is not None and env.leases.items.get(eid) is not lease:  # PLAN-031 integration: cancelled or replaced while this check waited
            return "failed", "lease_changed", False
        return await _restore_locked(eid, lease, env)


class Lease(NamedTuple):
    """One entity's lease: its end (UTC seconds), its newest grant or renew command, and the target it holds."""

    until: float
    command: str
    target: str  # on or level; HA-014: restore
    level: int | None
    prev: dict | None = None  # restore: the values to put back, {"temperature", "mode"} (a key the lease does not change is None); HA-021: see _FIELDS
    set: dict | None = None  # restore: the values the lease set, the same shape; the entity must still show them


def at_target(lease: Lease, seen: Seen | None) -> bool:
    """Whether the entity still shows the lease's target: on; for a level on with the brightness within 1 %; a restore lease: what it set."""
    if lease.target == "restore":
        return heat_shows(lease.set, seen)
    return reached(Command(lease.command, "", lease.target, lease.level, 0), seen)


_CHECKS = {"temperature": _heat_number, "low": _heat_number, "high": _heat_number, "mode": _heat_text, "preset": _heat_text, "option": _option_text, "value": _number}


def _spec(item: object, eid: object) -> dict | None:
    """A stored {"temperature", "mode"} of a restore lease, sanitised; None when it is not one. HA-021: a climate's may add preset, low and high; a
    select's is {"option"}, a number's {"value"}."""
    if not isinstance(item, dict) or not isinstance(eid, str):
        return None
    keys = set(item)
    if not (keys == {"option" if eid.startswith("select.") else "value"} if is_setting(eid)
            else is_heat(eid) and _HEAT_SPEC <= keys <= _HEAT_SPEC | ({"preset", "low", "high"} if eid.startswith("climate.") else set())):
        return None
    clean = {key: None if value is None else _CHECKS[key](value) for key, value in item.items()}
    if any(clean[key] is None and item[key] is not None for key in keys):
        return None
    return clean


class Leases:
    """AUTO-004: the entry's leases by entity id. Pure: the caller passes the wall clock and persists to_dict()."""

    def __init__(self, data: dict | None = None) -> None:
        self.items: dict[str, Lease] = {}
        for eid, item in (data or {}).items():
            try:
                lease = Lease(float(item["until"]), str(item["command"]), item["target"], item.get("level"), _spec(item.get("prev"), eid), _spec(item.get("set"), eid))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue  # a damaged entry is dropped, never guessed
            if not isinstance(eid, str):
                continue
            if _ENTITY_ID.fullmatch(eid) and lease.target in ("on", "level"):
                self.items[eid] = lease
            elif (restorable(eid) and lease.target == "restore" and lease.prev is not None and lease.set is not None and lease.prev.keys() == lease.set.keys()
                  and any(want is not None for want in lease.set.values())):
                self.items[eid] = lease

    def grant(self, eid: str, command: str, seconds: int, target: str, level: int | None, now: float) -> None:
        self.items[eid] = Lease(now + seconds, command, target, level)

    def grant_restore(self, eid: str, cmd: Command, seen: Seen, now: float) -> None:
        """HA-014: the lease of a leased set_temperature: it sets cmd's values (no temperature when the resulting mode is off) and keeps what
        the entity shows now as the values to put back. A lease already holding (the entity still shows what it set) keeps its own previous
        values: a rule changing its mind must never record the already-raised values as the previous ones.
        HA-021 (§2g): the same per key for a select's option, a number's value, a climate's preset and range (low, high); a climate's or water heater's
        lease keeps {"temperature", "mode"} and adds the other keys only while it sets them (the 0.19.0 store shape for a heat-only lease)."""
        heat = seen.heat
        new_set = {**(dict.fromkeys(_HEAT_SPEC) if is_heat(eid) else {}), **_sets(cmd, seen)}
        new_prev = {key: heat.get(_FIELDS[key]) if want is not None else None for key, want in new_set.items()}
        if new_set.get("preset") is not None and new_prev.get("preset") is None and "none" in heat.get("presetModes", ()):
            new_prev["preset"] = "none"  # HA-021 (§2g): no preset active is put back as the "none" preset (else the grant is refused, restore_invalid)
        old = self.items.get(eid)
        if old is not None and old.target == "restore" and heat_shows(old.set, seen):
            for key, held in old.set.items():
                if held is not None:
                    new_prev[key] = old.prev.get(key)
                    if new_set.get(key) is None:
                        new_set[key] = held
        keep = [key for key in new_set if key in _HEAT_SPEC or new_set[key] is not None]
        self.items[eid] = Lease(now + cmd.lease_s, cmd.id, "restore", None, {k: new_prev.get(k) for k in keep}, {k: new_set[k] for k in keep})

    def renew(self, eid: str, command: str, seconds: int, seen: Seen | None, now: float) -> str | None:
        """None when extended; else the failure reason (no lease, it ended, or the entity left its target)."""
        lease = self.items.get(eid)
        if lease is None:
            return "lease_cancelled"
        if lease.until <= now:
            return "lease_ended"
        if seen is None or seen.state in _UNAVAILABLE:
            return "entity_unavailable"  # momentarily gone: the lease stays and keeps running
        if not at_target(lease, seen):
            del self.items[eid]
            return "lease_cancelled"
        self.items[eid] = lease._replace(until=now + seconds, command=command)
        return None

    def cancel(self, eid: str) -> Lease | None:
        return self.items.pop(eid, None)

    def due(self, now: float) -> list[tuple[str, Lease]]:
        return [(eid, lease) for eid, lease in self.items.items() if lease.until <= now]

    def to_dict(self) -> dict:
        # an on/level lease keeps its 0.15 shape (no prev/set), so a downgrade reads it as before
        return {eid: {k: v for k, v in lease._asdict().items() if lease.target == "restore" or k not in ("prev", "set")} for eid, lease in self.items.items()}


def _finished(inflight: dict, eid: str, task: asyncio.Future) -> None:
    if inflight.get(eid, (None,))[0] is task:
        del inflight[eid]
    if not task.cancelled() and (err := task.exception()) is not None:
        _LOGGER.debug("Service call failed: %s", type(err).__name__)


# BAT-008 (0.4.0): one solax_modbus hub's direct VPP mode (docs/reviews/BAT-008.md, R1 + R2 + R3). Modes 4 (push power: charge
# or discharge), 5 (push power zero: hold), RA-004 (0.6.0) 7 (self-consume charge only: don't discharge) and the exit (0) only;
# the component resolves the hub's entities, SunnyHome never names one. Labels pinned to plugin_solax.py at tag 2026.09.4: the
# select's options and the sensor's labels differ.
SELECT_DISABLED, SELECT_PUSH, SELECT_ZERO = "Disabled", "Push Power-Positive/Negative Mode", "Push Power – Zero Mode"
SELECT_CHARGE_ONLY = "Self-Consume - Charge Only Mode"
TARGETS = {"charge": 4, "discharge": 4, "hold": 5, "no_discharge": 7}  # the mode of each apply kind
_SELECTS = {4: SELECT_PUSH, 5: SELECT_ZERO, 7: SELECT_CHARGE_ONLY}
MODE_LABELS = {label: mode for mode, label in enumerate((
    "Disabled", "Enabled Power Control", "Enabled Quantity Control", "Enabled SOC Target Control", "Push Power - P/N Mode",
    "Push Power - Zero Mode", "Self Consume - C/D Mode", "Self Consume - Charge Only Mode", "Individual Setting - Duration Mode",
    "Individual Setting - Target SOC Mode",
))}
_INVERTER_KEYS = frozenset({"id", "inverter", "kind", "powerW", "durationS", "ttlMs"})
INVERTER_WATCH_S = 30  # the sensor must show the target this long after the mode select (solax_modbus scans slower than 10 s)
INVERTER_CALL_S = 20  # a service call still running after this counts as failed (unknown once armed)
INVERTER_SHOW_S = 5  # how long the timeout number may take to show the written value
SETTLE_S = 60  # after the last dispatch the sensor's previous target, 0 or the target are ignored (it lags one scan)
EXIT_RETRY_S = 60  # a lease exit not yet seen on the sensor is sent again at most this often
EXIT_GIVE_UP_S = 86_400  # ... for at most a day after the lease end


class Remote(NamedTuple):
    """The resolved entities of the single solax_modbus hub."""

    mode: str  # select modbus_power_control_direct
    power: str  # number remotecontrol_push_mode_power_direct
    timeout: str  # number remotecontrol_timeout_direct
    sensor: str  # sensor modbus_power_control (input register 0x100)
    device_id: str | None = None  # the inverter device (model, sw_version)


class InverterCommand(NamedTuple):
    id: str
    op: str  # apply or exit
    kind: str | None  # charge, discharge, hold or no_discharge (apply only)
    power_w: int | None  # 100-1500 for charge and discharge
    duration_s: int | None  # 120-600 (apply): until - now when SunnyHome queued it
    ttl_ms: int


class InverterLease(NamedTuple):
    """The armed mode: the apply that armed it, its end (UTC seconds), the target, the previous target and the settle end."""

    command: str
    until: float
    target: int  # 4, 5 or 7
    prev: int | None
    settle: float
    reason: str | None = None  # option_off: ended early because the option was turned off (reported with that reason)


def parse_inverter(item: object) -> tuple[str | None, InverterCommand | None]:
    """Strict check of one claimed inverter command (the agreed interface); the optional keys may be absent or null."""
    if not isinstance(item, dict) or not isinstance(cid := item.get("id"), str) or not _UUID.fullmatch(cid):
        return None, None
    op, kind, power, duration, ttl = (item.get(key) for key in ("inverter", "kind", "powerW", "durationS", "ttlMs"))
    valid = (
        item.keys() <= _INVERTER_KEYS and type(ttl) is int and ttl >= 0
        and (
            (op == "exit" and kind is None and power is None and duration is None)
            or (op == "apply" and type(kind) is str and kind in TARGETS and type(duration) is int and 120 <= duration <= 600
                and (power is None if TARGETS[kind] != 4 else type(power) is int and 100 <= power <= 1500))
        )
    )
    return cid, (InverterCommand(cid, op, kind, power, duration, ttl) if valid else None)


def remote_mode(state: str | None) -> int | None:
    """The active mode 0-9 from the sensor's label; None when unavailable, unknown or not a known label."""
    return MODE_LABELS.get(state) if isinstance(state, str) else None


def _key_part(value: object) -> str | None:
    """A verified-key input as the server bounds it (1-64 printable ASCII characters), else null (then never verified)."""
    text = "" if value is None else str(value).strip()
    return text if 0 < len(text) <= 64 and text.isascii() and text.isprintable() else None


def remote_report(control: bool, state: str | None, reported: datetime | None, plugin: object, model: object, firmware: object) -> dict:
    """The "remote" object of the /states solax object: control (the inverter option on with all four entities), the mode, the
    hub's snapshot time and the verified-key inputs (null without control)."""
    return {
        "control": control, "mode": remote_mode(state), "lastReported": _iso(reported) if reported else None,
        "plugin": _key_part(plugin), "model": _key_part(model), "firmware": _key_part(firmware),
    }


class Inverter:
    """BAT-008: the hub's direct VPP mode under an HA-enforced lease. Pure: Home Assistant is reached through the callbacks.

    "failed" only before the lease is armed (nothing dispatched); once armed every failure is "unknown"; "done" when the mode
    sensor shows the target. The lease is the primary end of a mode: the tick sends the exit at its end unless it was renewed.
    """

    def __init__(self, lock: asyncio.Lock, remote: Callable[[], Remote | None], enabled: Callable[[], bool],
                 state: Callable[[str], str | None], dispatch: Callable[[str, str, dict], asyncio.Future],
                 save: Callable[[dict | None], Awaitable], report: Callable[[str, str, str | None], None],
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        self.lock, self.remote, self.enabled, self.state, self.dispatch = lock, remote, enabled, state, dispatch
        self._save, self.report, self.clock, self.wall, self.sleep = save, report, clock, wall, sleep
        self.lease: InverterLease | None = None
        self.restarted = False
        self.exit_sent: float | None = None  # monotonic time of the last unconfirmed lease exit
        self.ticking = False
        self.unloaded = False  # set when the entry unloads: a tick still waiting for the hub lock then writes nothing

    def load(self, data: object) -> None:
        """The stored lease; one that ended while HA was down is ended by the next tick and reported as a restart."""
        try:
            lease = InverterLease(str(data["command"]), float(data["until"]), int(data["target"]),
                                  None if data.get("prev") is None else int(data["prev"]), float(data["settle"]),
                                  None if data.get("reason") is None else str(data["reason"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            return  # none, or damaged: never guessed
        if lease.target in _SELECTS:
            self.lease, self.restarted = lease, lease.until <= self.wall()

    async def save(self) -> None:
        await self._save(None if self.lease is None else self.lease._asdict())

    async def _drop(self, outcome: str | None, reason: str | None) -> None:
        lease, self.lease, self.exit_sent, self.restarted = self.lease, None, None, False
        await self.save()
        if outcome:
            self.report(lease.command, outcome, reason)

    async def execute(self, cmd: InverterCommand, sent: float) -> tuple[str, str | None]:
        """Run one claimed command; (outcome, reason). The hub lock is the first await, so commands keep claim order."""
        until = deadline(sent, cmd.ttl_ms)
        async with self.lock:
            if cmd.op == "exit" and self.lease is not None and self.lease.until > self.wall():
                # An exit ends the lease whatever happens next: the tick then sends Disabled until the sensor shows it.
                self.lease = self.lease._replace(until=self.wall())
                await self.save()
            if cmd.op == "apply" and not self.enabled():
                return "failed", "inverter_control_off"
            if (remote := self.remote()) is None:
                return "failed", "entities_missing"
            # BAT-008B F1: the power and timeout numbers are solax_modbus local values, "unknown" until their first write (which
            # the apply makes), so only a missing or unavailable one refuses.
            numbers = (remote.power, remote.timeout) if cmd.op == "apply" else ()
            if any(self.state(e) in (None, "unavailable", "unknown") for e in (remote.mode, remote.sensor)) \
                    or any(self.state(e) in (None, "unavailable") for e in numbers):
                return "failed", "entity_unavailable"
            if self.clock() >= until:
                return "failed", "expired_on_arrival"
            mode = remote_mode(self.state(remote.sensor))
            if mode != 0 and (self.lease is None or mode not in (self.lease.target, self.lease.prev)):
                return "failed", "external_control"  # another controller (an EMS, an automation, the user) holds the inverter
            if cmd.op == "exit":
                return await self._exit(remote) if self.lease else ("done", None)
            return await self._apply(cmd, sent, remote)

    async def _apply(self, cmd: InverterCommand, sent: float, remote: Remote) -> tuple[str, str | None]:
        elapsed, now = self.clock() - sent, self.wall()
        target = TARGETS[cmd.kind]
        self.lease = InverterLease(cmd.id, now - elapsed + cmd.duration_s, target, self.lease and self.lease.target, now + SETTLE_S)
        self.exit_sent, self.restarted = None, False
        await self.save()  # armed before the first call: a crash right after it never leaves a mode without its lease
        timeout = math.ceil((cmd.duration_s - elapsed) / 60) * 60 + 60  # the inverter's own backstop; the entity steps by 60
        calls = [("number", "set_value", {"entity_id": remote.timeout, "value": timeout})]
        if target == 4:  # SolaX mode 4: positive = discharge
            power = cmd.power_w if cmd.kind == "discharge" else -cmd.power_w
            calls.append(("number", "set_value", {"entity_id": remote.power, "value": power}))
        for call in calls:
            if not await self._call(*call):
                return "unknown", "service_error"
        if not await self._shows(remote.timeout, timeout):
            return "unknown", "timeout_not_set"
        if not await self._call("select", "select_option", {"entity_id": remote.mode, "option": _SELECTS[target]}):
            return "unknown", "service_error"
        return await self._watch(remote.sensor, target)

    async def _exit(self, remote: Remote) -> tuple[str, str | None]:
        if not await self._call("select", "select_option", {"entity_id": remote.mode, "option": SELECT_DISABLED}):
            return "unknown", "service_error"
        result = await self._watch(remote.sensor, 0)
        if result[0] == "done":
            await self._drop(None, None)  # the exit's own result tells SunnyHome
        return result

    async def _call(self, domain: str, service: str, data: dict) -> bool:
        """One service call, awaited; False when it was refused (also synchronously), failed or ran too long."""
        try:
            task = self.dispatch(domain, service, data)
        except Exception as err:  # noqa: BLE001 - armed: any refusal ends the sequence as unknown
            _LOGGER.debug("Inverter call refused: %s", type(err).__name__)
            return False
        if self.lease is not None:
            self.lease = self.lease._replace(settle=self.wall() + SETTLE_S)
        end = self.clock() + INVERTER_CALL_S
        while not task.done():
            if self.clock() >= end:
                return False
            await self.sleep(POLL_S)
        return not task.cancelled() and task.exception() is None

    async def _shows(self, entity_id: str, value: float) -> bool:
        end = self.clock() + INVERTER_SHOW_S
        while True:
            try:
                if float(self.state(entity_id)) == value:
                    return True
            except (TypeError, ValueError):
                pass
            if self.clock() >= end:
                return False
            await self.sleep(POLL_S)

    async def _watch(self, sensor: str, mode: int) -> tuple[str, str | None]:
        end = self.clock() + INVERTER_WATCH_S
        while remote_mode(self.state(sensor)) != mode:
            if self.clock() >= end:
                return "unknown", "no_confirmation"
            await self.sleep(POLL_S)
        return "done", None

    async def tick(self, _now: object = None) -> None:
        """Every 5 s while a lease is stored: the sensor rules, and the exit at the lease end. One tick at a time; it waits for a
        running command and re-reads the lease under the hub lock."""
        if self.lease is None or self.ticking:
            return
        self.ticking = True
        try:
            await self._tick()
        finally:
            self.ticking = False

    async def _tick(self) -> None:
        async with self.lock:
            if self.unloaded:  # a reload's new instance owns the store now
                return
            lease, now = self.lease, self.wall()  # re-read under the lock: a renewal may have moved the end
            if lease is None:
                return
            remote = self.remote()
            mode = remote_mode(self.state(remote.sensor)) if remote else None
            if not self.enabled() and lease.reason != "option_off":
                # The option is off (setup after saving it): the lease ends now, so the exit below is sent until Disabled shows.
                lease = self.lease = lease._replace(until=min(lease.until, now), reason="option_off")
                await self.save()
            if mode not in (None, 0, lease.target, lease.prev):  # another mode: someone else took over; left as it is
                await self._drop("lease_cancelled", "outside_change")
            elif lease.until > now:
                # ponytail: the 5 s tick reads the sensor (no state listener); a flip shorter than a tick goes unseen.
                # The previous target is still ours (e.g. a re-apply whose mode select failed); another mode cancelled above.
                if now >= lease.settle and mode == 0:
                    await self._drop("lease_ended", "inverter")
            elif mode == 0:
                await self._drop("lease_ended", lease.reason or ("restart" if self.restarted else "expired"))
            elif now > lease.until + EXIT_GIVE_UP_S:
                await self._drop("lease_cancelled", "expired")
            elif remote and (self.exit_sent is None or self.clock() >= self.exit_sent + EXIT_RETRY_S):
                self.exit_sent = self.clock()
                await self._call("select", "select_option", {"entity_id": remote.mode, "option": SELECT_DISABLED})


# EV-004 (0.11.0, docs/reviews/EV-004.md §1, §2, §5 as amended by the pre review): the solax_modbus EV-charger hub (plugin
# solax_ev_charger). Its readings are posted as "wallbox" beside the inverter; with the Wallbox control option on SunnyHome may set its
# work mode (select charger_use_mode) under a stored lease that returns it to the wallbox's normal mode when SunnyHome stops renewing.
# Option texts pinned to plugin_solax_ev_charger.py at solax_modbus 2026.09.4. The session commands (control_command) are never used.
WALLBOX_KEYS = frozenset({
    "charge_power", "charge_power_l1", "charge_power_l2", "charge_power_l3", "charge_current_l1", "charge_current_l2", "charge_current_l3",
    "charge_voltage_l1", "charge_voltage_l2", "charge_voltage_l3", "charge_added", "charge_added_total",
})
WALLBOX_MODE_KEY = "charger_use_mode"
MAX_WALLBOX_READINGS = 16  # the server's bound
WALLBOX_OPTIONS = {"stop": "Stop", "fast": "Fast", "eco": "ECO", "green": "Green"}  # SunnyHome's mode -> the select's option
_WALLBOX_MODES = {option: mode for mode, option in WALLBOX_OPTIONS.items()}
GEAR_6A = "6A"  # GREEN and ECO always at their 6 A gear (EV-001 c.6: GREEN never at 3 A)
WALLBOX_SCAN_MAX_S = 30  # pre review P2-5: a slower hub scan reports the control off (the confirm window would end before a poll)
WALLBOX_CONFIRM_MARGIN_S = 10
WALLBOX_CALL_S = 20  # a select call still running after this counts as failed (unknown once armed)
_WALLBOX_COMMAND_KEYS = frozenset({"id", "wallbox", "mode", "normal", "durationS", "renew", "held", "ttlMs"})


def ev_plugin(plugin: str | None) -> bool:
    """EV-004: an EV-charger hub of solax_modbus (plugin solax_ev_charger); it is never an inverter."""
    return isinstance(plugin, str) and plugin.startswith("solax_ev")


def wallbox_mode(state: str | None) -> str | None:
    """The SunnyHome mode a charger_use_mode option shows (None: unknown, unavailable or another text)."""
    return _WALLBOX_MODES.get(state) if isinstance(state, str) else None


def wallbox_report(serial: str | None, readings: list, control: dict) -> dict:
    """The /states "wallbox" envelope of the EV-charger hub: (key, unit, state, last_reported) readings, at most the server's 16."""
    return {
        "integration": "solax_modbus", "serial": serial,
        "readings": [{"key": key, "unit": unit, "state": str(state), "lastReported": _iso(reported)}
                     for key, unit, state, reported in readings[:MAX_WALLBOX_READINGS]],
        "control": control,
    }


def wallbox_control(enabled: bool, reason: str | None, plugin: object, model: object, firmware: object) -> dict:
    """The envelope's "control": the Wallbox control option on with the three selects and a fast enough scan, why not, and the
    verified-key inputs (null without control)."""
    return {
        "enabled": enabled, "reason": None if enabled else reason,
        "plugin": _key_part(plugin) if enabled else None, "model": _key_part(model) if enabled else None,
        "firmware": _key_part(firmware) if enabled else None,
    }


class Charger(NamedTuple):
    """The resolved entities of the single EV-charger hub and its scan interval (seconds)."""

    mode: str  # select charger_use_mode (0x60D)
    green_gear: str  # select green_gear (0x60F)
    eco_gear: str  # select eco_gear (0x60E)
    scan_s: float = 15


class WallboxCommand(NamedTuple):
    id: str
    op: str  # apply or exit
    mode: str  # stop, fast, eco or green (an exit: the normal mode, stop or green)
    normal: str | None  # apply: the normal mode the lease returns to
    duration_s: int | None  # apply: 120-1800
    renew: bool | None  # apply: only extend the live lease of this mode (pre review P1-2)
    held: str | None  # exit: the mode SunnyHome last applied (pre review P2-1)
    ttl_ms: int


def parse_wallbox(item: object) -> tuple[str | None, WallboxCommand | None]:
    """Strict check of one claimed wallbox command; the optional keys may be absent or null."""
    if not isinstance(item, dict) or not isinstance(cid := item.get("id"), str) or not _UUID.fullmatch(cid):
        return None, None
    op, mode, normal, duration, renew, held, ttl = (item.get(key) for key in ("wallbox", "mode", "normal", "durationS", "renew", "held", "ttlMs"))
    valid = (
        item.keys() <= _WALLBOX_COMMAND_KEYS and type(ttl) is int and ttl >= 0 and mode in WALLBOX_OPTIONS
        and (
            (op == "apply" and normal in ("stop", "green") and type(duration) is int and 120 <= duration <= 1800 and type(renew) is bool
             and held is None)
            or (op == "exit" and mode in ("stop", "green") and normal is None and duration is None and renew is None
                and (held is None or held in WALLBOX_OPTIONS))
        )
    )
    return cid, (WallboxCommand(cid, op, mode, normal, duration, renew, held, ttl) if valid else None)


class WallboxLease(NamedTuple):
    """The armed mode: the apply that armed it, its end (UTC seconds), the target, the normal mode, the previous lease's target, the settle end."""

    command: str
    until: float
    target: str
    normal: str
    prev: str | None
    settle: float
    reason: str | None = None  # option_off: ended early because the option was turned off


class Wallbox:
    """EV-004: the EV charger's work mode under an HA-enforced lease. Pure: Home Assistant is reached through the callbacks.

    state(entity_id) gives (state text, last_reported as UTC seconds) or None. "failed" only before the lease is armed (nothing written);
    once armed every failure is "unknown"; "done" only when a real poll (the select's last_reported past the write's own echo) still
    shows every written option. The lease is the only end of a mode: the tick writes the normal mode at its end unless it was renewed.
    """

    def __init__(self, lock: asyncio.Lock, charger: Callable[[], Charger | None], enabled: Callable[[], bool],
                 state: Callable[[str], tuple[str | None, float | None] | None], dispatch: Callable[[str, str, dict], asyncio.Future],
                 save: Callable[[dict | None], Awaitable], echoes: dict, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        self.lock, self.charger, self.enabled, self.state, self.dispatch = lock, charger, enabled, state, dispatch
        self._save, self.echoes, self.clock, self.wall, self.sleep = save, echoes, clock, wall, sleep
        self.lease: WallboxLease | None = None
        self.exit_sent: float | None = None  # monotonic time of the last unconfirmed lease-end write
        self.ticking = False
        self.unloaded = False

    def load(self, data: object) -> None:
        """The stored lease; one that ended while HA was down is ended by the first tick once the select is known (pre review P2-2)."""
        try:
            lease = WallboxLease(str(data["command"]), float(data["until"]), str(data["target"]), str(data["normal"]),
                                 None if data.get("prev") is None else str(data["prev"]), float(data["settle"]),
                                 None if data.get("reason") is None else str(data["reason"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            return  # none, or damaged: never guessed (an exit's held mode still ends it, P2-1)
        if lease.target in WALLBOX_OPTIONS and lease.normal in ("stop", "green"):
            self.lease = lease

    async def save(self) -> None:
        await self._save(None if self.lease is None else self.lease._asdict())

    async def _drop(self) -> None:
        self.lease, self.exit_sent = None, None
        await self.save()

    def _shown(self, entity_id: str) -> tuple[str | None, float | None]:
        seen = self.state(entity_id)
        return seen if seen is not None else (None, None)

    def _gear(self, target: str, charger: Charger) -> str | None:
        return charger.green_gear if target == "green" else charger.eco_gear if target == "eco" else None

    async def execute(self, cmd: WallboxCommand, sent: float) -> tuple[str, str | None]:
        """Run one claimed command; (outcome, reason). The lock is the first await, so commands keep claim order."""
        until = deadline(sent, cmd.ttl_ms)
        async with self.lock:
            if cmd.op == "exit" and self.lease is not None and self.lease.until > self.wall():
                self.lease = self.lease._replace(until=self.wall())  # the exit ends the lease whatever happens next (before the ttl check)
                await self.save()
            if cmd.op == "apply" and not self.enabled():
                return "failed", "wallbox_control_off"
            if (charger := self.charger()) is None:
                return "failed", "entities_missing"
            shown = wallbox_mode(self._shown(charger.mode)[0])
            gear = self._gear(cmd.mode, charger)
            if shown is None or gear is not None and self._shown(gear)[0] in (None, "unavailable", "unknown"):
                return "failed", "entity_unavailable"
            if self.clock() >= until:
                return "failed", "expired_on_arrival"
            if cmd.op == "exit":
                return await self._exit(cmd, charger, shown)
            if cmd.renew:
                return await self._renew(cmd, sent, charger, shown)
            return await self._apply(cmd, sent, charger)

    async def _renew(self, cmd: WallboxCommand, sent: float, charger: Charger, shown: str) -> tuple[str, str | None]:
        """P1-2: only a live lease of this mode, still shown, with its 6 A gear (P3-1), is extended; never a write."""
        lease, gear = self.lease, self._gear(cmd.mode, charger)
        if lease is None or lease.target != cmd.mode or lease.until <= self.wall() or shown != cmd.mode \
                or gear is not None and self._shown(gear)[0] != GEAR_6A:
            return "failed", "outside_change"
        self.lease = lease._replace(until=self.wall() - (self.clock() - sent) + cmd.duration_s, normal=cmd.normal, reason=None)
        await self.save()
        return "done", None

    async def _apply(self, cmd: WallboxCommand, sent: float, charger: Charger) -> tuple[str, str | None]:
        elapsed, now = self.clock() - sent, self.wall()
        self.lease = WallboxLease(cmd.id, now - elapsed + cmd.duration_s, cmd.mode, cmd.normal, self.lease and self.lease.target,
                                  now + SETTLE_S)
        self.exit_sent = None
        await self.save()  # armed before the first write: a crash right after it never leaves a mode without its lease
        return await self._write(charger, cmd.mode)

    async def _exit(self, cmd: WallboxCommand, charger: Charger, shown: str) -> tuple[str, str | None]:
        lease = self.lease
        if lease is not None:
            if shown not in (lease.target, lease.prev):  # already normal, or a hand change: never overwritten
                await self._drop()
                return "done", None
            result = await self._write(charger, cmd.mode)
            if result[0] == "done":
                await self._drop()
            return result
        if cmd.held is not None and shown == cmd.held and shown != cmd.mode:  # P2-1: a lease lost to damaged storage still exits
            return await self._write(charger, cmd.mode)
        return "done", None  # the charger is not in what SunnyHome set: nothing to end

    async def _write(self, charger: Charger, mode: str) -> tuple[str, str | None]:
        """P2-5: the gear (GREEN/ECO at 6 A), then the mode, then one confirm window for all; P1-1: done only after a real poll."""
        writes = []
        if (gear := self._gear(mode, charger)) is not None and self._shown(gear)[0] != GEAR_6A:
            writes.append((gear, GEAR_6A))
        writes.append((charger.mode, WALLBOX_OPTIONS[mode]))
        for entity_id, option in writes:
            if not await self._call(entity_id, option):
                return "unknown", "service_error"
        echo = self._shown(charger.mode)[1]
        self.echoes[charger.mode] = echo  # the reading is withheld from posts until a real poll (P1-1)
        end = self.clock() + min(charger.scan_s + WALLBOX_CONFIRM_MARGIN_S, WALLBOX_SCAN_MAX_S + WALLBOX_CONFIRM_MARGIN_S)
        while True:
            state, reported = self._shown(charger.mode)
            if reported is not None and (echo is None or reported > echo):
                ok = all(self._shown(entity_id)[0] == option for entity_id, option in writes)
                return ("done", None) if ok else ("unknown", "not_applied")
            if self.clock() >= end:
                return "unknown", "no_confirmation"
            await self.sleep(POLL_S)

    async def _call(self, entity_id: str, option: str) -> bool:
        """One select_option, awaited; False when it was refused (also synchronously), failed or ran too long."""
        try:
            task = self.dispatch("select", "select_option", {"entity_id": entity_id, "option": option})
        except Exception as err:  # noqa: BLE001 - armed: any refusal ends the sequence as unknown
            _LOGGER.debug("Wallbox call refused: %s", type(err).__name__)
            return False
        if self.lease is not None:
            self.lease = self.lease._replace(settle=self.wall() + SETTLE_S)
        end = self.clock() + WALLBOX_CALL_S
        while not task.done():
            if self.clock() >= end:
                return False
            await self.sleep(POLL_S)
        return not task.cancelled() and task.exception() is None

    async def tick(self, _now: object = None) -> None:
        """Every 5 s while a lease is stored: a hand change drops it (no write), its end writes the normal mode until a real poll shows it."""
        if self.lease is None or self.ticking:
            return
        self.ticking = True
        try:
            await self._tick()
        finally:
            self.ticking = False

    async def _tick(self) -> None:
        async with self.lock:
            if self.unloaded:
                return
            lease, now = self.lease, self.wall()
            if lease is None:
                return
            charger = self.charger()
            state, reported = self._shown(charger.mode) if charger else (None, None)
            shown = wallbox_mode(state)
            if shown is None:
                return  # P2-2: HA starting (the select is unknown until its first poll) or the entities gone: neither drop nor write
            if not self.enabled() and lease.reason != "option_off":
                lease = self.lease = lease._replace(until=min(lease.until, now), reason="option_off")
                await self.save()
            echo = self.echoes.get(charger.mode)
            polled = echo is None or (reported is not None and reported > echo)
            if shown not in (lease.target, lease.prev, lease.normal):
                await self._drop()  # someone else changed it: left as it is
            elif lease.until > now:
                if now >= lease.settle and shown == lease.normal != lease.target and polled:
                    await self._drop()  # P1-2: a hand change to the normal mode mid-lease
            elif shown == lease.normal and polled:
                await self._drop()  # ended: the normal mode, seen by a real poll
            elif now > lease.until + EXIT_GIVE_UP_S:
                await self._drop()
            elif shown != lease.normal and (self.exit_sent is None or self.clock() >= self.exit_sent + EXIT_RETRY_S):
                self.exit_sent = self.clock()
                gear = self._gear(lease.normal, charger)
                if gear is not None and self._shown(gear)[0] != GEAR_6A:
                    await self._call(gear, GEAR_6A)
                if await self._call(charger.mode, WALLBOX_OPTIONS[lease.normal]):
                    self.echoes[charger.mode] = self._shown(charger.mode)[1]


# WALLBOX-005 (0.17.0, docs/reviews/WALLBOX-005.md): another wallbox, any charger Home Assistant already shows. The user picks its power sensor and the
# number that sets its charging current (A), optionally a switch, a session energy sensor and a status sensor. They are posted as "wallboxes"
# beside the SolaX EV path; with the Other wallbox control option on SunnyHome may set the current (set_current) under a stored restore lease
# (payload.GenericWallbox): the number's and the switch's previous values are put back at the lease end, only where they still show what the lease set.
GENERIC_READING_KEYS = ("power", "energy", "current", "status")
MAX_GENERIC_READINGS = 8  # the server's bound
GENERIC_MIN_A, GENERIC_MAX_A = 6, 32  # the effective range is the number's own min and max cut to these
GENERIC_NEAR_A = 0.5  # a charger shows a written current within half an ampere (integrations round to their step)
GENERIC_CONFIRM_S = WALLBOX_SCAN_MAX_S + WALLBOX_CONFIRM_MARGIN_S  # how long a real poll of a written entity is awaited
_STATUS = re.compile(r"[A-Za-z0-9 _.-]{1,32}")
_PLATFORM = re.compile(r"[a-z0-9_]{1,40}")
_GENERIC_COMMAND_KEYS = frozenset({"id", "wallbox", "mode", "amps", "durationS", "renew", "ttlMs"})
_GENERIC_REQUIRED = frozenset({"id", "wallbox", "mode", "amps", "ttlMs"})  # a restore sends amps null and no duration or renew


class OtherWallbox(NamedTuple):
    """The entity ids the user picked in the Other wallbox options step (power and number are required, the rest may be None)."""

    number: str
    switch: str | None
    power: str
    energy: str | None
    status: str | None


class GenericConfig(NamedTuple):
    """The number's resolved range: min_a and max_a are None while its own min and max are not usable (range_invalid)."""

    number: str
    switch: str | None
    min_a: float | None
    max_a: float | None
    allows_zero: bool  # the number's own min is 0, so a switch-less STOP may write 0


def generic_range(low: object, high: object) -> tuple[float, float, bool] | None:
    """The effective range from the number entity's min and max attributes: cut to 6..32 A; (min, max, own min is 0) or None when an
    attribute is no finite number or nothing is left of the range."""
    own_low, own_high = _heat_number(low), _heat_number(high)
    if own_low is None or own_high is None:
        return None
    start, end = max(own_low, GENERIC_MIN_A), min(own_high, GENERIC_MAX_A)
    return (start, end, own_low <= 0) if start <= end else None


def other_wallbox_error(power: str | None, current: str | None, attrs: Mapping | None) -> tuple[str, str] | None:
    """The options step's check: None when both required entities are empty (no other wallbox) or valid; else (field, error code), with field
    power or current: needs_both when only one is set, number_unit when the number is not in A, number_range when it has no usable min and max
    (attrs are the number's state attributes, None when it is not in the state machine)."""
    if not power and not current:
        return None
    if not power or not current:
        return ("power" if not power else "current", "needs_both")
    attrs = attrs or {}
    if attrs.get("unit_of_measurement") != "A":
        return "current", "number_unit"
    return None if generic_range(attrs.get("min"), attrs.get("max")) else ("current", "number_range")


def _amp(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 1)


def generic_control(option: bool, present: bool, rng: tuple[float, float, bool] | None, platform: object, has_switch: bool) -> dict:
    """The "control" of a wallboxes item: the option on, the number present and its range usable; why not; the range; the number's integration; the switch; whether its own minimum is 0."""
    reason = "option_off" if not option else "entities_missing" if not present else "range_invalid" if rng is None else None
    return {
        "enabled": reason is None, "reason": reason, "minA": _amp(rng[0]) if rng else None, "maxA": _amp(rng[1]) if rng else None,
        "platform": platform if isinstance(platform, str) and _PLATFORM.fullmatch(platform) else None, "switch": has_switch,
        "zero": bool(rng and rng[2]),  # the number's own minimum is 0: a switch-less STOP may write 0
    }


def generic_report(number: str, switch: str | None, readings: list, control: dict) -> dict:
    """One item of the /states "wallboxes" list: (key, unit, state, last_reported) readings of power, energy, current (the number's own state) and
    status (only text of at most 32 allowed characters), bounded as the server does."""
    items = []
    for key, unit, state, reported in readings:
        text = str(state)
        if key == "status" and not _STATUS.fullmatch(text):
            continue
        items.append({"key": key, "unit": unit if isinstance(unit, str) and _utf16(unit) <= MAX_UNIT else "",
                      "state": text if _utf16(text) <= MAX_STATE else "unknown", "lastReported": _iso(reported)})
    return {"kind": "ha_generic", "number": number, "switch": switch, "readings": items[:MAX_GENERIC_READINGS], "control": control}


class GenericCommand(NamedTuple):
    id: str
    mode: str  # stop, fast, eco or green: the rule's mode, audit only
    amps: float | None  # apply: 0-32 (0: STOP); restore: None
    duration_s: int | None  # apply: 120-1800
    renew: bool | None  # apply: extends the live lease (and writes amps when they differ)
    ttl_ms: int


def parse_generic(item: object) -> tuple[str | None, GenericCommand | None]:
    """Strict check of one claimed set_current command: the apply form has amps, durationS and renew, the restore form amps null and neither
    of the others (absent or null)."""
    if not isinstance(item, dict) or not isinstance(cid := item.get("id"), str) or not _UUID.fullmatch(cid):
        return None, None
    mode, amps, duration, renew, ttl = (item.get(key) for key in ("mode", "amps", "durationS", "renew", "ttlMs"))
    valid = (
        _GENERIC_REQUIRED <= item.keys() <= _GENERIC_COMMAND_KEYS and item["wallbox"] == "set_current" and type(ttl) is int and ttl >= 0
        and mode in WALLBOX_OPTIONS
        and (amps is None and duration is None and renew is None
             or isinstance(amps, (int, float)) and not isinstance(amps, bool) and 0 <= amps <= GENERIC_MAX_A
             and type(duration) is int and 120 <= duration <= 1800 and type(renew) is bool)
    )
    return cid, (GenericCommand(cid, mode, None if amps is None else float(amps), duration, renew, ttl) if valid else None)


class GenericLease(NamedTuple):
    """The restore lease: the apply that armed it, its end (UTC seconds), what it set and what to put back ({"amps", "on"}; a key the lease
    does not hold is None), and the settle end (a hand change is not looked for before it)."""

    command: str
    until: float
    set: dict
    prev: dict
    settle: float
    reason: str | None = None  # option_off: ended early because the option was turned off


def _generic_spec(item: object) -> dict | None:
    """A stored {"amps", "on"} of a lease, sanitised; None when it is not one."""
    if not isinstance(item, dict) or item.keys() != {"amps", "on"}:
        return None
    amps, on = item["amps"], item["on"]
    if amps is not None and _heat_number(amps) is None or on is not None and type(on) is not bool:
        return None
    return {"amps": None if amps is None else float(amps), "on": on}


def generic_lease(data: object) -> GenericLease | None:
    """The stored lease; None when there is none or it is damaged (never guessed)."""
    try:
        lease = GenericLease(str(data["command"]), float(data["until"]), _generic_spec(data["set"]), _generic_spec(data["prev"]), float(data["settle"]),
                             None if data.get("reason") is None else str(data["reason"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return lease if lease.set is not None and lease.prev is not None and any(want is not None for want in lease.set.values()) else None


def generic_shows(spec: dict, amps: float | None, on: bool | None) -> bool:
    """Whether the charger shows every value the spec holds (a None key is not held)."""
    return all((amps is not None and abs(amps - want) < GENERIC_NEAR_A) if key == "amps" else on is want for key, want in spec.items() if want is not None)


def generic_verdict(lease: GenericLease, amps: float | None, on: bool | None) -> tuple[str, dict | None]:
    """The lease end per held key (heat_verdict's rule): at its previous value it is done, at the value the lease set it is put back, at anything
    else somebody changed it by hand. ("restore", {key: value to write}) | ("ended", None) | ("cancelled", None)."""
    back = {}
    for key, want in lease.set.items():
        if want is None:
            continue
        old = lease.prev.get(key)
        if old is None:  # nothing to put back for this key
            if not generic_shows({key: want}, amps, on):
                return "cancelled", None
        elif generic_shows({key: old}, amps, on):
            continue
        elif generic_shows({key: want}, amps, on):
            back[key] = old
        else:
            return "cancelled", None
    return ("restore", back) if back else ("ended", None)


def generic_back_calls(cfg: GenericConfig, back: dict) -> list[tuple[str, str, dict, str, object]]:
    """The calls that put the values of back (a verdict's) in place: the number first, the switch last. (domain, service, data, key, wanted)."""
    calls = []
    if "amps" in back:
        calls.append(("number", "set_value", {"entity_id": cfg.number, "value": back["amps"]}, "amps", back["amps"]))
    if "on" in back and cfg.switch:
        calls.append(("switch", "turn_on" if back["on"] else "turn_off", {"entity_id": cfg.switch}, "on", back["on"]))
    return calls


class GenericWallbox:
    """WALLBOX-005: another charger's current (a number, with an optional switch) under an HA-enforced restore lease. Pure: Home Assistant is
    reached through the callbacks, like Wallbox.

    config() gives the resolved entities and range (None: not set up or the number is gone); state(entity_id) gives (state text, last_reported as
    UTC seconds) or None. "failed" only before the lease is armed (nothing written); once armed every failure is "unknown"; "done" only when a real
    poll (a written entity's last_reported past the write's own echo) shows every written value. The lease is the only end of a change: the tick
    puts the previous values back at its end unless it was renewed, and only where the charger still shows what the lease set.
    """

    def __init__(self, lock: asyncio.Lock, config: Callable[[], GenericConfig | None], enabled: Callable[[], bool],
                 state: Callable[[str], tuple[str | None, float | None] | None], dispatch: Callable[[str, str, dict], asyncio.Future],
                 save: Callable[[dict | None], Awaitable], echoes: dict, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], Awaitable] = asyncio.sleep) -> None:
        self.lock, self.config, self.enabled, self.state, self.dispatch = lock, config, enabled, state, dispatch
        self._save, self.echoes, self.clock, self.wall, self.sleep = save, echoes, clock, wall, sleep
        self.lease: GenericLease | None = None
        self.exit_sent: float | None = None  # monotonic time of the last unconfirmed lease-end write
        self.ticking = False
        self.unloaded = False

    def load(self, data: object) -> None:
        self.lease = generic_lease(data)

    async def save(self) -> None:
        await self._save(None if self.lease is None else self.lease._asdict())

    async def _drop(self) -> None:
        self.lease, self.exit_sent = None, None
        await self.save()

    def _shown(self, entity_id: str) -> tuple[str | None, float | None]:
        seen = self.state(entity_id)
        return seen if seen is not None else (None, None)

    def _read(self, cfg: GenericConfig) -> tuple[float | None, bool | None, bool]:
        """(current in A, switch on, both known): a number that is no number or a switch that is neither on nor off is not known."""
        try:
            amps = float(self._shown(cfg.number)[0])
        except (TypeError, ValueError):
            amps = None
        text = self._shown(cfg.switch)[0] if cfg.switch else None
        on = {"on": True, "off": False}.get(text) if cfg.switch else None
        return amps, on, amps is not None and math.isfinite(amps) and (not cfg.switch or on is not None)

    def _target(self, amps: float, cfg: GenericConfig) -> dict:
        """What a command sets: 0 is STOP, the switch off with the number left alone (a switch-less charger gets 0 on its number); else the
        amps, with the switch on."""
        if amps == 0:
            return {"amps": None, "on": False} if cfg.switch else {"amps": 0.0, "on": None}
        return {"amps": float(amps), "on": True if cfg.switch else None}

    def _allowed(self, amps: float, cfg: GenericConfig) -> bool:
        if amps == 0:
            return bool(cfg.switch) or cfg.allows_zero
        return cfg.min_a is not None and cfg.min_a - 1e-9 <= amps <= cfg.max_a + 1e-9

    def _plan(self, cfg: GenericConfig, spec: dict, amps: float | None, on: bool | None) -> list[tuple[str, str, dict, str, object]]:
        """The calls that make the charger show spec: the switch on first, the number (not when it shows it already), a switch off."""
        calls = []
        if spec["on"] is True and on is False:
            calls.append(("switch", "turn_on", {"entity_id": cfg.switch}, "on", True))
        if spec["amps"] is not None and not generic_shows({"amps": spec["amps"]}, amps, on):
            calls.append(("number", "set_value", {"entity_id": cfg.number, "value": spec["amps"]}, "amps", spec["amps"]))
        if spec["on"] is False and on is True:
            calls.append(("switch", "turn_off", {"entity_id": cfg.switch}, "on", False))
        return calls

    async def execute(self, cmd: GenericCommand, sent: float) -> tuple[str, str | None]:
        """Run one claimed command; (outcome, reason). The lock is the first await, so commands keep claim order."""
        until = deadline(sent, cmd.ttl_ms)
        restore = cmd.amps is None
        async with self.lock:
            if restore and self.lease is not None and self.lease.until > self.wall():
                self.lease = self.lease._replace(until=self.wall())  # a restore ends the lease whatever happens next (before the ttl check)
                await self.save()
            if not restore and not self.enabled():
                return "failed", "wallbox_control_off"
            if restore and self.lease is None:
                return "done", None  # nothing SunnyHome set
            if (cfg := self.config()) is None:
                return "failed", "entities_missing"
            amps, on, known = self._read(cfg)
            if not known:
                return "failed", "entity_unavailable"
            if self.clock() >= until:
                return "failed", "expired_on_arrival"
            if restore:
                return await self._restore(cfg, amps, on)
            if not self._allowed(cmd.amps, cfg):
                return "failed", "outside_range"
            spec = self._target(cmd.amps, cfg)
            if cmd.renew:
                return await self._renew(cmd, sent, cfg, spec, amps, on)
            return await self._apply(cmd, sent, cfg, spec, amps, on)

    def _merged(self, spec: dict, lease: GenericLease | None) -> dict:
        """The lease's set after a change: the new values, and what the old lease held that this one leaves alone (the number under a STOP)."""
        return {key: want if want is not None else (lease.set[key] if lease else None) for key, want in spec.items()}

    async def _apply(self, cmd: GenericCommand, sent: float, cfg: GenericConfig, spec: dict, amps: float | None, on: bool | None) -> tuple[str, str | None]:
        elapsed, now, old = self.clock() - sent, self.wall(), self.lease
        keep = old if old is not None and generic_shows(old.set, amps, on) else None  # still shows what it set: its previous values stay the real ones
        prev = keep.prev if keep else {"amps": amps, "on": on}
        self.lease = GenericLease(cmd.id, now - elapsed + cmd.duration_s, self._merged(spec, keep), prev, now + SETTLE_S)
        self.exit_sent = None
        await self.save()  # armed before the first write: a crash right after it never leaves a change without its lease
        return await self._write(cfg, self._plan(cfg, spec, amps, on))

    async def _renew(self, cmd: GenericCommand, sent: float, cfg: GenericConfig, spec: dict, amps: float | None, on: bool | None) -> tuple[str, str | None]:
        """Only a live lease whose values are still shown is extended; other amps are written under the same lease (its previous values stay)."""
        lease, now = self.lease, self.wall()
        if lease is None or lease.until <= now or not generic_shows(lease.set, amps, on):
            return "failed", "outside_change"
        until, merged = now - (self.clock() - sent) + cmd.duration_s, self._merged(spec, lease)
        if merged == lease.set:
            self.lease = lease._replace(until=until, reason=None)
            await self.save()
            return "done", None
        self.lease = lease._replace(until=until, set=merged, settle=now + SETTLE_S, reason=None)
        self.exit_sent = None
        await self.save()
        return await self._write(cfg, self._plan(cfg, spec, amps, on))

    async def _restore(self, cfg: GenericConfig, amps: float | None, on: bool | None) -> tuple[str, str | None]:
        verdict, back = generic_verdict(self.lease, amps, on)
        if verdict == "restore":
            result = await self._write(cfg, generic_back_calls(cfg, back))
            if result[0] != "done":
                return result  # the lease stays (its end is now): the tick puts it back until a real poll shows it
        await self._drop()  # ended, or changed by hand: nothing written
        return "done", None

    async def _write(self, cfg: GenericConfig, calls: list) -> tuple[str, str | None]:
        """The calls in order, then one confirm window: done only after a real poll of every written entity shows its value."""
        for domain, service, data, _, _ in calls:
            if not await self._call(domain, service, data):
                return "unknown", "service_error"
        if not calls:
            return "done", None
        echoes = {data["entity_id"]: self._shown(data["entity_id"])[1] for _, _, data, _, _ in calls}
        self.echoes.update(echoes)  # the readings are withheld from posts until a real poll
        end = self.clock() + GENERIC_CONFIRM_S
        while True:
            if all(self._polled(entity_id, echo) for entity_id, echo in echoes.items()):
                amps, on, _ = self._read(cfg)
                return ("done", None) if all(generic_shows({key: want}, amps, on) for _, _, _, key, want in calls) else ("unknown", "not_applied")
            if self.clock() >= end:
                return "unknown", "no_confirmation"
            await self.sleep(POLL_S)

    def _polled(self, entity_id: str, echo: float | None) -> bool:
        reported = self._shown(entity_id)[1]
        return reported is not None and (echo is None or reported > echo)

    async def _call(self, domain: str, service: str, data: dict) -> bool:
        """One service call, awaited; False when it was refused (also synchronously), failed or ran too long."""
        try:
            task = self.dispatch(domain, service, data)
        except Exception as err:  # noqa: BLE001 - armed: any refusal ends the sequence as unknown
            _LOGGER.debug("Other wallbox call refused: %s", type(err).__name__)
            return False
        if self.lease is not None:
            self.lease = self.lease._replace(settle=self.wall() + SETTLE_S)
        end = self.clock() + WALLBOX_CALL_S
        while not task.done():
            if self.clock() >= end:
                return False
            await self.sleep(POLL_S)
        return not task.cancelled() and task.exception() is None

    async def tick(self, _now: object = None) -> None:
        """Every 5 s while a lease is stored: a hand change drops it (no write), its end puts the previous values back until a real poll shows them."""
        if self.lease is None or self.ticking:
            return
        self.ticking = True
        try:
            await self._tick()
        finally:
            self.ticking = False

    async def _tick(self) -> None:
        async with self.lock:
            if self.unloaded:
                return
            lease, now = self.lease, self.wall()
            if lease is None or (cfg := self.config()) is None:
                return
            amps, on, known = self._read(cfg)
            if not known:
                return  # HA starting (the entities are unknown until their first poll) or gone: neither drop nor write
            if not self.enabled() and lease.reason != "option_off":
                lease = self.lease = lease._replace(until=min(lease.until, now), reason="option_off")
                await self.save()
            verdict, back = generic_verdict(lease, amps, on)
            if lease.until > now:
                if verdict == "cancelled" and now >= lease.settle:
                    await self._drop()  # someone else changed it: left as it is
                return
            held = [cfg.number if key == "amps" else cfg.switch for key, want in lease.set.items() if want is not None]
            if verdict == "cancelled" or verdict == "ended" and all(self._polled(e, self.echoes.get(e)) for e in held):
                await self._drop()  # changed by hand, or ended: the previous values, seen by a real poll
            elif now > lease.until + EXIT_GIVE_UP_S:
                await self._drop()
            elif verdict == "restore" and (self.exit_sent is None or self.clock() >= self.exit_sent + EXIT_RETRY_S):
                self.exit_sent = self.clock()
                for domain, service, data, _, _ in generic_back_calls(cfg, back):
                    if not await self._call(domain, service, data):
                        break
                    self.echoes[data["entity_id"]] = self._shown(data["entity_id"])[1]

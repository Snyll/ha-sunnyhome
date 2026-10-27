"""Pure helpers for the SunnyHome device API (stdlib only, no Home Assistant imports)."""

from __future__ import annotations

import asyncio
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
})

# SRC-009 (0.7.2): profile sensors solax_modbus ships disabled that SunnyHome turns on itself (exact home consumption).
AUTO_ENABLE_KEYS = frozenset({"e_charge_today"})

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
# HA-005: picked sensors. The server's rules again (one item outside them refuses the whole post): the id within the rule bound
# (64 characters), these device classes, at most 32, state and unit lengths counted like the server (UTF-16 code units).
MAX_SENSORS = 32
MAX_STATE = 64
MAX_UNIT = 16
SENSOR_CLASSES = frozenset({"temperature", "power", "energy"})
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
    """A shared switch or light, as read from the HA state machine."""

    entity_id: str
    name: str | None
    state: str
    brightness: int | None  # HA scale 0-255
    supports_brightness: bool
    last_changed: datetime | None


def _name(name: str | None, entity_id: str) -> str:
    """The server's name rule: printable, trimmed, at most 64 characters; else the entity id."""
    name = (name or "").strip()
    return ((name if name.isprintable() else "") or entity_id)[:MAX_NAME].rstrip()


def _entity(e: Entity, control: Collection[str]) -> dict:
    domain = e.entity_id.split(".", 1)[0]
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


def _utf16(text: str) -> int:
    """A length as the server counts it (UTF-16 code units), so a non-BMP character cannot pass here and fail there."""
    return len(text.encode("utf-16-le")) // 2


def _sensors(sensors: Sequence[Sensor]) -> list[dict]:
    """The sensor items the server accepts: valid ids once, the three classes, bounded state and unit, at most 32."""
    items, seen = [], set()
    for s in sensors:
        if not SENSOR_ID.fullmatch(s.entity_id) or s.device_class not in SENSOR_CLASSES or s.entity_id in seen:
            continue
        seen.add(s.entity_id)
        state = str(s.state)
        items.append({
            "entityId": s.entity_id,
            "name": _name(s.name, s.entity_id),
            "deviceClass": s.device_class,
            "state": state if _utf16(state) <= MAX_STATE else "unknown",
            "unit": s.unit if isinstance(s.unit, str) and _utf16(s.unit) <= MAX_UNIT else None,
            "lastReported": _iso(s.last_reported),
        })
    return items[:MAX_SENSORS]


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
    """
    solax = None if readings is None else {
        "serial": serial,
        "readings": [
            {"key": key, "unit": unit, "state": str(state), "lastReported": _iso(reported)}
            for key, unit, state, reported in readings[:MAX_READINGS]
        ],
    }
    items = [_entity(e, control) for e in entities if _ENTITY_ID.fullmatch(e.entity_id)]
    if solax is not None and integration != "solax_modbus":
        body = {"sentAt": _iso(sent_at), "inverter": {"integration": integration, **solax}, "entities": items[:MAX_ENTITIES]}
    else:
        if solax is not None and remote is not None:
            solax["remote"] = remote
        if solax is not None and hub_settings is not None:
            solax["settings"] = hub_settings
        if solax is not None and hub_info is not None:
            solax.update(hub_info)
        body = {"sentAt": _iso(sent_at), "solax": solax, "entities": items[:MAX_ENTITIES]}
    if sensors is not None:  # expand -> contract: only while sensors are ticked (a backend before HA-005 refuses the key)
        body["sensors"] = _sensors(sensors)
    if wallbox is not None:  # EV-004: only with an EV-charger hub (a backend before EV-004 refuses a solax_modbus wallbox)
        body["wallbox"] = wallbox
    return body


# Commands (HA-003): POST /api/ha/v1/commands/claim and /commands/result. AUTO-004 (0.3.0): a claim with the X-SunnyHome-Leases
# header gets a sixth key, leaseS: the lease length of an automation on/level or a lease_renew; lease_cancel ends a lease.

TTL_MAX_MS = 30_000  # HA runs no command later than 30 s after sending the claim, whatever ttlMs the server sends
BUSY_S = 30  # a later command is refused while the entity's previous service call runs longer than this
WATCH_S = 10  # how long the target state is awaited after the call was issued
POLL_S = 0.2
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_COMMAND_KEYS = frozenset({"id", "entityId", "action", "level", "ttlMs"})
_LEASED_KEYS = _COMMAND_KEYS | {"leaseS"}
LEASE_MIN_S, LEASE_MAX_S = 60, 3600
_LOGGER = logging.getLogger(__name__)


class Command(NamedTuple):
    id: str
    entity_id: str
    action: str  # on, off, level; AUTO-004: lease_renew, lease_cancel
    level: int | None  # 1-100 for level, else None
    ttl_ms: int
    lease_s: int | None = None  # AUTO-004: the lease of an automation on/level, or the renewal's length


class Seen(NamedTuple):
    """An entity's current state, as the executor needs it."""

    state: str
    brightness: int | float | None  # HA scale 0-255
    dimmable: bool
    assumed: bool


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
    save: Callable[[], Awaitable] | None = None  # persists the leases; awaited before a leased service call
    wall: Callable[[], float] = time.time  # the lease clock (UTC seconds), persisted across restarts


def parse_command(item: object) -> tuple[str | None, Command | None]:
    """Strict check of one claimed command: (id, command); command None if invalid, id None if the id is."""
    if not isinstance(item, dict) or not isinstance(cid := item.get("id"), str) or not _UUID.fullmatch(cid):
        return None, None
    entity_id, action, level, ttl, lease = (item.get(key) for key in ("entityId", "action", "level", "ttlMs", "leaseS"))
    leasing = type(lease) is int and LEASE_MIN_S <= lease <= LEASE_MAX_S
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


def reached(cmd: Command, seen: Seen | None) -> bool:
    """The target state: on or off; for a level on with the brightness within 1 %."""
    if seen is None:
        return False
    if cmd.action != "level":
        return seen.state == cmd.action
    b = seen.brightness
    return seen.state == "on" and isinstance(b, (int, float)) and abs(round(b * 100 / 255) - cmd.level) <= 1


async def execute(cmd: Command, until: float, env: Env) -> tuple[str, str | None, bool]:
    """Run one command; (outcome, reason, assumedState) of the result.

    Single writer per entity across entries and reloads: the per-entity lock keeps claim order, and a
    command waits for the entity's previous service call (never cancelled) before its own checks.
    "failed" only when nothing was dispatched; after dispatch only "done" (target observed) or "unknown".
    AUTO-004: a leased on/level records its lease (saved) before its checks and drops it again when refused;
    every other command for the entity ends the entity's lease; lease_renew and lease_cancel call no service.
    """
    eid = cmd.entity_id
    async with env.locks.setdefault(eid, asyncio.Lock()):
        if prior := env.inflight.get(eid):
            task, started = prior
            while not task.done():
                if env.clock() >= started + BUSY_S:
                    return "failed", "entity_busy", False
                await env.sleep(POLL_S)
        if cmd.action in ("lease_renew", "lease_cancel"):
            return await _lease_command(cmd, until, env)
        leased = cmd.lease_s is not None and env.leases is not None
        if env.leases is not None:
            if leased:
                env.leases.grant(eid, cmd.id, cmd.lease_s, cmd.action, cmd.level, env.wall())
                await env.save()  # saved before the call: a crash right after it never leaves an on without its lease
            elif env.leases.cancel(eid) is not None:
                await env.save()
        # No await from here to the dispatch: the checks hold when the call starts.
        refusal = _refusal(cmd, until, env)
        if refusal is None:
            try:
                task = env.dispatch(*service_call(cmd))
            except Exception as err:  # raised before the service ran, so nothing was switched
                _LOGGER.debug("Service call refused: %s", type(err).__name__)
                refusal = "service_error"
        if refusal is not None:
            if leased:
                env.leases.cancel(eid)
                await env.save()
            return "failed", refusal, False
        env.inflight[eid] = (task, env.clock())
        task.add_done_callback(partial(_finished, env.inflight, eid))
        end = env.clock() + WATCH_S
        while not (task.done() and (task.cancelled() or task.exception() is not None)):
            if reached(cmd, seen := env.state(eid)):
                return "done", "assumed_state" if seen.assumed else None, seen.assumed
            if env.clock() >= end:
                return "unknown", "no_confirmation", False
            await env.sleep(POLL_S)
        return "unknown", "service_error", False  # the call failed after it started (e.g. ServiceValidationError)


def _refusal(cmd: Command, until: float, env: Env) -> str | None:
    """Why a command is refused before any service call, or None."""
    eid = cmd.entity_id
    if env.clock() >= until:
        return "expired_on_arrival"
    if not env.allowed(eid):
        return "not_controllable"
    seen = env.state(eid)
    if seen is None or seen.state in ("unavailable", "unknown"):
        return "entity_unavailable"
    if cmd.action == "level" and not (eid.startswith("light.") and seen.dimmable):
        return "not_dimmable"
    return None


async def _lease_command(cmd: Command, until: float, env: Env) -> tuple[str, str | None, bool]:
    """AUTO-004: a renewal extends only an active lease whose entity is still at its target; a cancel ends the lease (done also when none)."""
    if cmd.action == "lease_cancel":
        if env.leases is not None and env.leases.cancel(cmd.entity_id) is not None:
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


class Lease(NamedTuple):
    """One entity's lease: its end (UTC seconds), its newest grant or renew command, and the target it holds."""

    until: float
    command: str
    target: str  # on or level
    level: int | None


def at_target(lease: Lease, seen: Seen | None) -> bool:
    """Whether the entity still shows the lease's target: on; for a level on with the brightness within 1 %."""
    return reached(Command(lease.command, "", lease.target, lease.level, 0), seen)


class Leases:
    """AUTO-004: the entry's leases by entity id. Pure: the caller passes the wall clock and persists to_dict()."""

    def __init__(self, data: dict | None = None) -> None:
        self.items: dict[str, Lease] = {}
        for eid, item in (data or {}).items():
            try:
                lease = Lease(float(item["until"]), str(item["command"]), item["target"], item.get("level"))
            except (KeyError, TypeError, ValueError):
                continue  # a damaged entry is dropped, never guessed
            if _ENTITY_ID.fullmatch(eid) and lease.target in ("on", "level"):
                self.items[eid] = lease

    def grant(self, eid: str, command: str, seconds: int, target: str, level: int | None, now: float) -> None:
        self.items[eid] = Lease(now + seconds, command, target, level)

    def renew(self, eid: str, command: str, seconds: int, seen: Seen | None, now: float) -> str | None:
        """None when extended; else the failure reason (no lease, it ended, or the entity left its target)."""
        lease = self.items.get(eid)
        if lease is None:
            return "lease_cancelled"
        if lease.until <= now:
            return "lease_ended"
        if seen is None or seen.state in ("unavailable", "unknown"):
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
        return {eid: lease._asdict() for eid, lease in self.items.items()}


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

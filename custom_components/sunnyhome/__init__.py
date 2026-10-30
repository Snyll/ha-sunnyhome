"""SunnyHome: every 10 s, push the solax_modbus profile sensors and the shared devices to SunnyHome.

Only the profile sensors (PROFILE_KEYS, or SMB-004 the brand's BRAND_KEYS) of a single solax_modbus hub, the inverter serial, the
switches and lights and the temperature, power and energy sensors (HA-005) the user
ticked in the options are sent; nothing else of this Home Assistant is read or sent.
While the options' control list is non-empty, commands for those entities are claimed
from SunnyHome and run (HA-003). AUTO-004 (0.3.0): an automation "on" carries a lease; the
entity is switched off at the lease end unless SunnyHome renewed it. Leases are stored, so
they survive restarts and reloads; a manual command or a change from another context ends
one, and lease ends are reported to SunnyHome. BAT-008 (0.4.0): with the separate inverter option on, SunnyHome may put the
single solax_modbus hub into a direct VPP mode (charge, discharge or hold; RA-004 (0.6.0): don't discharge, mode 7) under the
same kind of stored lease (payload.Inverter).
BAT-003C: the hub's remote mode (sensor modbus_power_control) is a reading and is sent with the inverter option on or off.
ISET-004 (0.5.0): the state of the hub's setting entities named by its integration's mapping profile (payload.SETTINGS_ENTITIES) is
sent too; it is only read, never set.
GW-002 (0.7.0): the single inverter integration entry may instead be the core goodwe integration; its profile sensors
(payload.GOODWE_KEYS) and serial are posted as "inverter". GoodWe has no remote control and no settings here.
HA-008 (0.7.3): the solax_modbus hub's plugin and its inverter device's manufacturer ride in "solax"; the serial is the inverter
device's.
SMB-004 (0.8.0): a Growatt, Solis or Sofar hub of solax_modbus sends its brand's sensors (payload.BRAND_KEYS); a Growatt hub's
serial is its serialnumber sensor and the device serial number rides as "family". These hubs are read only: no remote, settings or
inverter control. Any other plugin's hub still sends the SolaX keys, which SunnyHome refuses.
SMB-007 (0.9.0): such a hub also sends the state of the setting entities its brand's settings profile names
(payload.SETTINGS_PROFILES, profiles/home_assistant.solax_modbus_<brand>.json), still only read: no remote, no control.
DEYE-003 (0.10.0): the single inverter entry may instead be a Deye inverter of the ha-solarman custom integration (domain solarman:
an entry whose inverter device's manufacturer starts with "Deye"; Home Assistant core's Solarman plugs and meters never count). Its
sensors (payload.DEYE_KEYS) are posted as "inverter" once the Device sensor's Serial Number attribute holds a valid serial (never the
registry serial, which falls back to the logger's); its entities are never shared. Read only: no solarman service is ever called.
HA-014 (0.16.0): a ticked heat pump or boiler (climate, water_heater) that supports a target temperature may be commanded: set_temperature
(bounded by its own min, max and step) and set_mode (one of its modes) through the same claim route. A rule's change carries a lease that
stores the previous target and mode (a "restore" lease, payload.Lease); at its end, on a cancel or when SunnyHome stops renewing, the
previous values are put back where the entity still shows the values the lease set, and a change by hand ends the lease without a restore.
EV-004 (0.11.0): a solax_modbus hub of the EV-charger plugin (solax_ev_charger) is never an inverter entry, so it sits beside the
inverter hub. Its readings and work mode are posted as "wallbox"; with the separate Wallbox control option on SunnyHome may set its
work mode (charger_use_mode, GREEN and ECO at their 6 A gear) under a stored lease that returns it to the wallbox's normal mode when
SunnyHome stops renewing (payload.Wallbox). If Home Assistant itself is down the charger keeps the mode. One EV-charger hub only.
WALLBOX-005 (0.17.0): any other charger Home Assistant shows: the options' Other wallbox step names its power sensor and the number that sets
its charging current (A), optionally a switch, a session energy sensor and a status sensor. They are posted as "wallboxes" beside the SolaX
EV path; with the separate control option on SunnyHome may set the current (set_current) under a stored restore lease that puts the number's
and the switch's previous values back at its end, only where they still show what the lease set (payload.GenericWallbox). If Home Assistant
itself is down, the charger keeps its setting.
HA-016 (0.18.0): each shared entity except a person carries its Home Assistant device's name and manufacturer, so SunnyHome's
picker groups one device's entities together.
HA-021 (0.20.0): whole devices ticked in the options send every allowed entity of the device (switches, lights, binary sensors, heat
entities, selects, numbers and numeric sensors; no disabled, hidden or non-sensor diagnostic entity), new ones included, and every
controllable one of them is controllable (no separate control list) and watched for leases. Devices of energy integrations, devices the
Energy dashboard uses and the Other wallbox entities are never expanded. The component posts the 0.19.0 shape until SunnyHome's answer
carries "contract":2, then the 0.20.0 shape: gzip, the HA device id and model, config categories, state classes, Energy dashboard roles,
climate humidity, presets and range targets, and up to 512 entities and 512 sensors. Rule leases also hold a select's option, a number's
value, a climate's preset and range targets, and put them back at the lease end where nobody changed them by hand.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from functools import partial

import aiohttp
import voluptuous as vol

from homeassistant import loader
from homeassistant.components.light import brightness_supported
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import Context, CoreState, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import storage
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import payload
from .const import (
    CONF_BINARY, CONF_CONTROL, CONF_DEVICES, CONF_ENTITIES, CONF_HEAT, CONF_INVERTER, CONF_OTHER_WALLBOX, CONF_OW_CURRENT, CONF_OW_ENERGY, CONF_OW_POWER,
    CONF_OW_STATUS, CONF_OW_SWITCH, CONF_PEOPLE, CONF_SENSORS, CONF_WALLBOX, DOMAIN, VERSION,
)

_LOGGER = logging.getLogger(__name__)
SOLAX = "solax_modbus"
GOODWE = "goodwe"  # GW-002
INVERTERS = (SOLAX, GOODWE)  # the inverter integrations: exactly one entry across them is read, their entities are never shared
SOLARMAN = "solarman"  # DEYE-003: ha-solarman (Deye) and HA core's Solarman plugs share this domain; only Deye entries count
ISSUE = "multiple_hubs"  # GW-002: several inverter integration entries (of one kind or mixed)
SERIAL_KEYS = ("measured_power", "inverter_power")
SHARED_DOMAINS = ("switch", "light")
# HA-021: a whole device of one of these integrations is never expanded (an inverter's work mode or export limit, a battery's reserve must never
# become controllable); extended as users report. INVERTERS and the Deye entries (unshared_entries) count too.
ENERGY_PLATFORMS = frozenset({*INVERTERS, "huawei_solar", "sungrow", "victron", "victron_ble", "fronius", "solax", "solis", "growatt_server", "sunsynk",
                              "solaredge", "sma", "enphase_envoy", "powerwall", "tesla_fleet", "teslemetry",
                              # EV chargers: their current and mode are SunnyHome's wallbox control, never a whole device's
                              "easee", "wallbox", "ocpp", "zaptec", "goecharger", "go_echarger", "go_e", "tesla_wall_connector", "openevse", "keba",
                              "ohme", "myenergi", "peblar", "alfen_wallbox", "v2c", "nrgkick", "evcc_intg"})
CONTROL_DOMAINS = frozenset({*SHARED_DOMAINS, *payload.HEAT_DOMAINS, *payload.SETTING_DOMAINS})  # HA-021: what a whole device shares control of (D2)
NOT_NUMERIC = frozenset({"enum", "timestamp", "date"})  # HA-021: sensor classes that are never a reading
ISSUE_OLD = "server_older"  # HA-021: devices are ticked but SunnyHome does not take the 0.20.0 shape yet
ISSUE_TRIM = "devices_trimmed"  # HA-021: whole devices pass the caps or the byte budget
RELOAD_S = 30  # HA-021: an entity registry change of a shared device reloads the entry after this quiet time
BACKOFF_MIN, BACKOFF_MAX = 10, 300
LEASE_TICK_S = 5  # how often due leases are checked
REPORT_WINDOW_S = 900  # a lease report is retried this long, then dropped (SunnyHome reconciles it by time)
LEASE_OFF_RETRY_S = 60  # a lease off that was not confirmed is sent again at most this often
LEASE_GIVE_UP_S = 86_400  # a lease whose device is not seen off a day after its end is dropped
# BAT-008: the direct VPP entities of the hub, by role: (domain, unique id key after "<hub name>_").
REMOTE_KEYS = {
    "mode": ("select", "modbus_power_control_direct"), "power": ("number", "remotecontrol_push_mode_power_direct"),
    "timeout": ("number", "remotecontrol_timeout_direct"), "sensor": ("sensor", "modbus_power_control"),
}
# EV-004: the EV-charger hub's selects, by role (unique id key after "<hub name>_").
CHARGER_KEYS = {"mode": "charger_use_mode", "green_gear": "green_gear", "eco_gear": "eco_gear"}
CHARGER_LOCK = "solax_modbus:ev"  # its own hub lock: inverter and charger commands never wait for each other
OTHER_WALLBOX_LOCK = "other_wallbox"  # WALLBOX-005: its own lock too
_warned_chargers = False


def inverter_entries(hass: HomeAssistant) -> list[tuple[str, ConfigEntry]]:
    """GW-002: the (domain, entry) of every inverter integration entry (solax_modbus, goodwe; DEYE-003: Deye solarman entries).
    EV-004: a solax_modbus EV-charger hub is not one."""
    found = [(domain, entry) for domain in INVERTERS for entry in hass.config_entries.async_entries(domain)
             if not (domain == SOLAX and payload.ev_plugin(hub_plugin(entry)))]
    return found + [(SOLARMAN, entry) for entry in deye_entries(hass)]


def charger_hub(hass: HomeAssistant) -> ConfigEntry | None:
    """EV-004: the single solax_modbus EV-charger hub, or None (none, or several: logged once, nothing sent or controlled)."""
    global _warned_chargers
    hubs = [entry for entry in hass.config_entries.async_entries(SOLAX) if payload.ev_plugin(hub_plugin(entry))]
    if len(hubs) > 1 and not _warned_chargers:
        _warned_chargers = True
        _LOGGER.warning("Several EV charger hubs: SunnyHome reads and controls one EV charger only, so it reads none")
    return hubs[0] if len(hubs) == 1 else None


def charger_scan(hub: ConfigEntry) -> float:
    """The EV-charger hub's scan interval in seconds (solax_modbus' default 15 s; its selects are in the default group)."""
    try:
        return float(hub.options.get("scan_interval") or hub.data.get("scan_interval") or 15)
    except (TypeError, ValueError):
        return 15.0


def solax_charger(hass: HomeAssistant) -> payload.Charger | None:
    """EV-004: the three selects of the single EV-charger hub resolved by unique id; None unless all are there."""
    hub = charger_hub(hass)
    if hub is None:
        return None
    prefix = f"{hub.options.get('name') or hub.data.get('name')}_"
    wanted = {("select", prefix + key): role for role, key in CHARGER_KEYS.items()}
    found = {}
    for ent in er.async_entries_for_config_entry(er.async_get(hass), hub.entry_id):
        if ent.platform == SOLAX and not ent.disabled and (role := wanted.get((ent.domain, ent.unique_id))):
            found[role] = ent.entity_id
    return payload.Charger(**found, scan_s=charger_scan(hub)) if len(found) == len(CHARGER_KEYS) else None


def other_wallbox(entry: ConfigEntry) -> payload.OtherWallbox | None:
    """WALLBOX-005: the entities of the Other wallbox options, or None unless both the power sensor and the current number are set."""
    options = entry.options
    power, number = options.get(CONF_OW_POWER), options.get(CONF_OW_CURRENT)
    if not power or not number:
        return None
    return payload.OtherWallbox(number, options.get(CONF_OW_SWITCH) or None, power, options.get(CONF_OW_ENERGY) or None, options.get(CONF_OW_STATUS) or None)


def generic_config(hass: HomeAssistant, entry: ConfigEntry) -> payload.GenericConfig | None:
    """WALLBOX-005: the Other wallbox's number and switch with the range from the number's own min and max; None while it is not set up or the
    number is not in the state machine. A range that is not usable is carried as None (an apply is then refused outside_range)."""
    wallbox = other_wallbox(entry)
    state = hass.states.get(wallbox.number) if wallbox else None
    if state is None:
        return None
    rng = payload.generic_range(state.attributes.get("min"), state.attributes.get("max")) or (None, None, False)
    return payload.GenericConfig(wallbox.number, wallbox.switch, *rng)


def deye_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """DEYE-003: the one Deye rule, for counting, dispatch, the repair issue, BAT-008 and sharing: a solarman entry whose inverter
    device's manufacturer starts with "Deye" (ha-solarman sets "Deye"; core Solarman plugs and meters "SOLARMAN")."""
    return [entry for entry in hass.config_entries.async_entries(SOLARMAN)
            if payload.deye_manufacturer(getattr(inverter_device(hass, entry), "manufacturer", None))]


def unshared_entries(hass: HomeAssistant) -> frozenset[str]:
    """DEYE-003: the config entry ids whose entities are never shared (the Deye entries; a core Solarman plug stays pickable)."""
    return frozenset(entry.entry_id for entry in deye_entries(hass))


def inverter_device(hass: HomeAssistant, entry: ConfigEntry) -> dr.DeviceEntry | None:
    """HA-008: the inverter device of an inverter integration entry (its manufacturer and serial number), or None; DEYE-003 reuses
    it. solax_modbus marks it with the identifier (solax_modbus, <hub name>, "inverter"); otherwise it is the entry's one device
    that hangs below no other device."""
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    found = [d for d in devices if any(len(i) == 3 and i[0] == entry.domain and i[2] == "inverter" for i in d.identifiers)]
    found = found or [d for d in devices if d.via_device_id is None]
    return found[0] if len(found) == 1 else None


def shareable(ent: er.RegistryEntry, excluded: frozenset[str] = frozenset()) -> bool:
    """The allow-list rule for shared devices: an enabled switch or light that is not of an inverter integration. DEYE-003: excluded
    holds the Deye entry ids (unshared_entries)."""
    return (ent.domain in SHARED_DOMAINS and ent.platform not in INVERTERS and not ent.disabled
            and not (excluded and ent.config_entry_id in excluded))


def sensor_shareable(ent: er.RegistryEntry, excluded: frozenset[str] = frozenset()) -> bool:
    """The allow-list rule for picked sensors (HA-005): an enabled sensor of a payload.SENSOR_CLASSES class (HA-011: temperature, power, energy, battery, voltage,
    current, humidity, gas, water), not of an inverter integration."""
    return (
        ent.domain == "sensor" and ent.platform not in INVERTERS and not ent.disabled
        and not (excluded and ent.config_entry_id in excluded)
        and (ent.device_class or ent.original_device_class) in payload.SENSOR_CLASSES
        and payload.SENSOR_ID.fullmatch(ent.entity_id) is not None
    )


def binary_shareable(ent: er.RegistryEntry, excluded: frozenset[str] = frozenset()) -> bool:
    """HA-012: an enabled binary sensor of a payload.BINARY_CLASSES class (door, window, garage door, opening, motion, occupancy), not of an
    inverter integration."""
    return (
        ent.domain == "binary_sensor" and ent.platform not in INVERTERS and not ent.disabled
        and not (excluded and ent.config_entry_id in excluded)
        and (ent.device_class or ent.original_device_class) in payload.BINARY_CLASSES
        and payload._STATE_ID.fullmatch(ent.entity_id) is not None
    )


def person_shareable(ent: er.RegistryEntry) -> bool:
    """HA-012: an enabled person entity (shared as home or away only, see payload._person_state)."""
    return ent.domain == "person" and not ent.disabled and payload._STATE_ID.fullmatch(ent.entity_id) is not None


def heat_shareable(ent: er.RegistryEntry, excluded: frozenset[str] = frozenset()) -> bool:
    """HA-013: an enabled climate or water_heater entity (a heat pump or boiler) that is not of an inverter integration; read only."""
    return (
        ent.domain in payload.HEAT_DOMAINS and ent.platform not in INVERTERS and not ent.disabled
        and not (excluded and ent.config_entry_id in excluded)
        and payload._STATE_ID.fullmatch(ent.entity_id) is not None
    )


def heat_controllable(state) -> bool:
    """HA-014: a heat entity supports a target temperature (feature bit 1 of climate and of water_heater); one that is not in the state machine
    or unavailable keeps its last decision (true), so a lease end is never skipped just because the device blinked. HA-021 (D7): a climate with a
    low and a high target (feature bit 2; a water heater's bit 2 is its operation mode) is controllable too."""
    if state is None or state.state in ("unavailable", "unknown"):
        return True
    features = state.attributes.get("supported_features")
    return isinstance(features, int) and (features & 1 == 1 or features & 2 == 2 and getattr(state, "domain", None) != "water_heater")


def setting_shareable(ent: er.RegistryEntry, excluded: frozenset[str] = frozenset()) -> bool:
    """HA-021: an enabled select or number of a whole device, not of an energy integration, within the server's id rule."""
    return (ent.domain in payload.SETTING_DOMAINS and ent.platform not in ENERGY_PLATFORMS and not ent.disabled
            and not (excluded and ent.config_entry_id in excluded) and payload._STATE_ID.fullmatch(ent.entity_id) is not None)


def device_sensor_shareable(ent: er.RegistryEntry, excluded: frozenset[str] = frozenset()) -> bool:
    """HA-021 (D3): a numeric sensor of a whole device, of any class within the server's rule or none: one of SENSOR_CLASSES, or one with a state
    class or a unit (never an enum, timestamp or date)."""
    kind = ent.device_class or ent.original_device_class
    return (
        ent.domain == "sensor" and ent.platform not in ENERGY_PLATFORMS and not ent.disabled and not (excluded and ent.config_entry_id in excluded)
        and payload.SENSOR_ID.fullmatch(ent.entity_id) is not None and (kind is None or payload.ANY_CLASS.fullmatch(kind) is not None)
        and kind not in NOT_NUMERIC and (kind in payload.SENSOR_CLASSES or bool((ent.capabilities or {}).get("state_class")) or bool(ent.unit_of_measurement))
    )


def _expands(ent: er.RegistryEntry, excluded: frozenset[str]) -> bool:
    """HA-021: the existing *_shareable rules plus the select, number and device sensor rules (a person has no device)."""
    return (shareable(ent, excluded) or binary_shareable(ent, excluded) or heat_shareable(ent, excluded) or setting_shareable(ent, excluded)
            or device_sensor_shareable(ent, excluded))


def whole_device(registry: er.EntityRegistry, device_id: str, excluded: frozenset[str], roles: dict, ow: frozenset[str]) -> list[er.RegistryEntry] | None:
    """HA-021: the entities a whole-device share sends, by entity id; None for a device that is never expanded: one with an entity of an energy
    integration (ENERGY_PLATFORMS, a Deye entry) or one the Energy dashboard uses (D6). Disabled, hidden and diagnostic entities (sensors excepted, D3)
    and the Other wallbox entities are left out."""
    ents = er.async_entries_for_device(registry, device_id, include_disabled_entities=True)
    if not ents or any(ent.platform in ENERGY_PLATFORMS or (excluded and ent.config_entry_id in excluded) or ent.entity_id in roles for ent in ents):
        return None
    return sorted((ent for ent in ents if ent.entity_id not in ow and not ent.disabled and ent.hidden_by is None
                   and (ent.entity_category != "diagnostic" or ent.domain == "sensor") and _expands(ent, excluded)), key=lambda ent: ent.entity_id)


def energy_roles(hass: HomeAssistant) -> dict[str, str]:
    """HA-021 (D6): {entity id: grid | solar | battery} of the Energy dashboard, as last read by watch_energy (empty until then or on failure)."""
    return hass.data.get(DOMAIN, {}).get("energy_roles", {})


async def watch_energy(hass: HomeAssistant) -> None:
    """HA-021 (D6): reads the Energy dashboard's sources once per Home Assistant run and again on every change (an internal HA API, so guarded:
    on any failure there are no roles, and SunnyHome's name fallback applies). Never in collect(): the manager is async."""
    shared = hass.data.setdefault(DOMAIN, {})
    if "energy_roles" in shared:
        return
    shared["energy_roles"] = {}
    try:
        from homeassistant.components.energy import data as energy_data  # noqa: PLC0415 - internal, imported only here

        manager = await energy_data.async_get_manager(hass)

        async def update() -> None:
            shared["energy_roles"] = payload.energy_roles(manager.data)
        await update()
        manager.async_listen_updates(update)
    except Exception as err:  # noqa: BLE001 - an internal API: anything may change
        _LOGGER.debug("Energy dashboard not read: %s", type(err).__name__)


def other_wallbox_ids(options) -> frozenset[str]:
    """HA-021: the Other wallbox entities (WALLBOX-005), never part of a whole-device share."""
    return frozenset(options.get(key) for key in (CONF_OW_POWER, CONF_OW_CURRENT, CONF_OW_SWITCH, CONF_OW_ENERGY, CONF_OW_STATUS) if options.get(key))


def device_entities(hass: HomeAssistant, entry: ConfigEntry) -> list[er.RegistryEntry]:
    """HA-021: the entities of the ticked whole devices, once each, by entity id (no cap: the pusher caps what it sends)."""
    devices = entry.options.get(CONF_DEVICES) or []
    if not devices:
        return []
    registry, excluded, roles, ow = er.async_get(hass), unshared_entries(hass), energy_roles(hass), other_wallbox_ids(entry.options)
    found = {}
    for device_id in devices:
        for ent in whole_device(registry, device_id, excluded, roles, ow) or ():
            found.setdefault(ent.entity_id, ent)
    return [found[key] for key in sorted(found)]


def main_device(hass: HomeAssistant, device_id: str | None):
    """The device registry entry of a device id; for a child device (HA 2026.10) its parent, where the manufacturer, model, serial number and
    firmware live (reading them on a child is deprecated, removed in 2027.9). Plain async_get(id): its keywords do not exist in HA 2025.1."""
    devices = dr.async_get(hass)
    device = devices.async_get(device_id) if device_id else None
    parent = getattr(device, "parent_device_id", None)  # only a child device has it
    return devices.async_get(parent) if isinstance(parent, str) else device


def all_devices(hass: HomeAssistant) -> list:
    """Every device registry entry: HA 2026.10's devices view yields the entries when iterated (using it as a mapping, .values() included, is
    deprecated and stops working in 2027.9); an older HA's dict (2025.1) yields the ids, which async_get resolves."""
    devices = dr.async_get(hass)
    return [devices.async_get(item) if isinstance(item, str) else item for item in devices.devices]


def device_choices(hass: HomeAssistant, ow: frozenset[str]) -> list[tuple[str, str, list[er.RegistryEntry]]]:
    """HA-021: (device id, label, entities) of every device a whole-device share may take, by label; an excluded device is never offered."""
    registry, excluded, roles = er.async_get(hass), unshared_entries(hass), energy_roles(hass)
    found = []
    for device in all_devices(hass):
        if device is None:
            continue
        if ents := whole_device(registry, device.id, excluded, roles, ow):
            name = device.name_by_user or device.name or device.id
            maker = " ".join(text for text in (device.manufacturer, device.model) if text)
            found.append((device.id, f"{name} ({maker})" if maker else name, ents))
    return sorted(found, key=lambda choice: choice[1].casefold())


def device_control(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """HA-021 (D2): the controllable kinds of the whole devices (switch, light, heat, select, number), whatever their state: the lease watcher's part."""
    return {ent.entity_id for ent in device_entities(hass, entry) if ent.domain in CONTROL_DOMAINS}


def controllable(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """The control list as enforced: ticked for control, still shared and still shareable. HA-014: a ticked heat entity that supports a target.
    HA-021 (D2): every controllable entity of a whole device too, without a control list."""
    registry = er.async_get(hass)
    shared = entry.options.get(CONF_ENTITIES, [])[: payload.MAX_ENTITIES]
    heat = entry.options.get(CONF_HEAT, [])[: payload.MAX_ENTITIES]
    excluded = unshared_entries(hass)
    return {
        e for e in entry.options.get(CONF_CONTROL, [])
        if (e in shared and (ent := registry.async_get(e)) is not None and shareable(ent, excluded))
        or (e in heat and (ent := registry.async_get(e)) is not None and heat_shareable(ent, excluded) and heat_controllable(hass.states.get(e)))
    } | {e for e in device_control(hass, entry) if e.split(".", 1)[0] not in payload.HEAT_DOMAINS or heat_controllable(hass.states.get(e))}


def watched(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """The entities the lease watcher follows: the ticked control list (PLAN-031 integration: not the filtered one, so a heat entity that blinked is
    still watched) and HA-021 the controllable entities of the whole devices."""
    return sorted(set(entry.options.get(CONF_CONTROL, [])[: payload.MAX_ENTITIES]) | device_control(hass, entry))


def hub_plugin(hub: ConfigEntry) -> str | None:
    """HA-008: the solax_modbus hub entry's plugin option (see payload.plugin_name)."""
    return payload.plugin_name(hub.options.get("plugin") or hub.data.get("plugin"))


def solax_entities(hass: HomeAssistant) -> tuple[dict[str, str], str | None]:
    """BAT-008: the direct VPP entities of the single solax_modbus hub found by role (resolved by unique id), and the inverter device.
    SMB-004: none for a hub of another brand's plugin (no control of Growatt, Solis or Sofar)."""
    hubs = [entry for entry in hass.config_entries.async_entries(SOLAX) if not payload.ev_plugin(hub_plugin(entry))]  # EV-004
    # GW-002: only while it is the single inverter entry; DEYE-003: a Deye entry is another inverter (a core Solarman plug is not)
    if len(hubs) != 1 or hass.config_entries.async_entries(GOODWE) or deye_entries(hass):
        return {}, None
    if not payload.solax_plugin(hub_plugin(hubs[0])):
        return {}, None
    prefix = f"{hubs[0].options.get('name') or hubs[0].data.get('name')}_"
    wanted = {(domain, prefix + key): role for role, (domain, key) in REMOTE_KEYS.items()}
    found, device = {}, None
    for ent in er.async_entries_for_config_entry(er.async_get(hass), hubs[0].entry_id):
        if ent.platform == SOLAX and not ent.disabled and (role := wanted.get((ent.domain, ent.unique_id))):
            found[role] = ent.entity_id
            device = ent.device_id if role == "sensor" else device
    return found, device


def solax_remote(hass: HomeAssistant) -> payload.Remote | None:
    """BAT-008: the four direct VPP entities of the single solax_modbus hub; None unless all are there."""
    found, device = solax_entities(hass)
    return payload.Remote(**found, device_id=device) if len(found) == len(REMOTE_KEYS) else None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await watch_energy(hass)  # HA-021 (D6): before the first push, never in collect()
    pusher = Pusher(hass, entry)
    await pusher.load()  # HA-021: the stored contract, so a restart posts the 0.20.0 shape at once
    entry.async_on_unload(async_track_time_interval(hass, pusher.tick, timedelta(seconds=10), name="SunnyHome push"))
    entry.async_on_unload(entry.add_update_listener(_reload))
    runner = CommandRunner(hass, entry)
    # AUTO-004: the leases are kept with or without a control list (an empty list drops them) and whatever the token's state,
    # so a lease still ends its device after an unlink.
    await runner.start_leases()
    # BAT-008: the inverter option alone also claims; EV-004: so does the Wallbox control option; WALLBOX-005: and the Other wallbox control option;
    # HA-021: and any whole device (its control is shared with it, D2)
    if entry.options.get(CONF_CONTROL) or entry.options.get(CONF_INVERTER) or entry.options.get(CONF_WALLBOX) or entry.options.get(CONF_OTHER_WALLBOX) \
            or entry.options.get(CONF_DEVICES):
        # An entry background task: HA cancels it, and the commands it started, on unload or reload.
        entry.async_create_background_task(hass, runner.run(), "SunnyHome commands")
    if entry.options.get(CONF_DEVICES):  # HA-021: a new, changed or removed entity of a shared device reloads the entry (debounced), so the watcher follows
        pending: list = []
        follow = (frozenset(entry.options[CONF_DEVICES]), frozenset(watched(hass, entry)))
        entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, partial(_registry_changed, hass, entry, follow, pending)))
        entry.async_on_unload(lambda: pending and pending.pop()())
    return True


@callback
def _registry_changed(hass: HomeAssistant, entry: ConfigEntry, follow: tuple[frozenset, frozenset], pending: list, event) -> None:
    """HA-021: an entity registry change of a ticked device (or of a watched entity, which a removal leaves no device to tell) reloads the entry
    RELOAD_S after the last such change."""
    devices, entities = follow
    entity_id = event.data.get("entity_id")
    ent = er.async_get(hass).async_get(entity_id) if entity_id else None
    old = (event.data.get("changes") or {}).get("device_id")
    if entity_id not in entities and (ent is None or ent.device_id not in devices) and old not in devices:
        return
    if pending:
        pending.pop()()
    pending.append(async_call_later(hass, RELOAD_S, callback(lambda _now: hass.config_entries.async_schedule_reload(entry.entry_id))))


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Saving the options (or a new token at reauth) restarts the pusher."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ir.async_delete_issue(hass, DOMAIN, ISSUE)
    for key in (ISSUE_OLD, ISSUE_TRIM):  # HA-021: raised again by the next push if still true
        ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """AUTO-004: removing the integration forgets its leases; the devices stay as they are. BAT-008: an inverter lease gets a
    best-effort exit first."""
    await storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.leases").async_remove()
    await storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.contract").async_remove()  # HA-021
    hass.data.get(DOMAIN, {}).get("sent", {}).pop(entry.entry_id, None)  # HA-021: the entities the last post sent
    store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.inverter")
    if await store.async_load() and (remote := solax_remote(hass)):
        try:
            await hass.services.async_call(
                "select", "select_option", {"entity_id": remote.mode, "option": payload.SELECT_DISABLED}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - best effort; the inverter's own timeout is the backstop
            _LOGGER.debug("Inverter exit at removal failed: %s", type(err).__name__)
    await store.async_remove()
    # EV-004: a stored wallbox lease gets a best-effort write of its normal mode first, only while the charger still shows what the lease
    # set (post review P3-4: never over a hand change)
    store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.wallbox")
    if (data := await store.async_load()) and (charger := solax_charger(hass)) and data.get("normal") in payload.WALLBOX_OPTIONS \
            and (state := hass.states.get(charger.mode)) is not None \
            and payload.wallbox_mode(state.state) in (data.get("target"), data.get("prev")):
        try:
            await hass.services.async_call(
                "select", "select_option", {"entity_id": charger.mode, "option": payload.WALLBOX_OPTIONS[data["normal"]]}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - best effort
            _LOGGER.debug("Wallbox exit at removal failed: %s", type(err).__name__)
    await store.async_remove()
    # WALLBOX-005: the same for a stored Other wallbox lease: the previous values go back only where the charger still shows what the lease set
    store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.wallbox_generic")
    if (lease := payload.generic_lease(await store.async_load())) and (cfg := generic_config(hass, entry)):
        number, switch = hass.states.get(cfg.number), hass.states.get(cfg.switch) if cfg.switch else None
        try:
            amps, on = float(number.state), {"on": True, "off": False}.get(switch.state) if switch else None
        except (AttributeError, TypeError, ValueError):
            amps = on = None
        if amps is not None and (cfg.switch is None or on is not None):
            for domain, service, data, _, _ in payload.generic_back_calls(cfg, payload.generic_verdict(lease, amps, on)[1] or {}):
                try:
                    await hass.services.async_call(domain, service, data, blocking=True)
                except Exception as err:  # noqa: BLE001 - best effort
                    _LOGGER.debug("Other wallbox restore at removal failed: %s", type(err).__name__)
                    break
    await store.async_remove()


class Pusher:
    """Collects the profile readings and the shared devices and posts them, with backoff and reauth."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.url = f"{entry.data[CONF_URL]}/api/ha/v1/states"
        self.headers = {"Authorization": f"Bearer {entry.data[CONF_TOKEN]}"}
        self.busy = self.stopped = self.warned = False
        self.states_off = False  # HA-012: set by a 422 on a post with binary sensors or people; kept until the entry reloads
        self.heat_off = False  # HA-013: the same for heat pumps and boilers, on its own, so a refused heat item never silences the others
        self.wallboxes_off = False  # WALLBOX-005: the same for the Other wallbox, on its own
        self.devices_off = False  # HA-016: the same for the entities' HA device names, on their own
        self.air_off = False  # HA-017: the same for a climate's fan and vane pairs, on their own
        # HA-021: the 0.20.0 shape only after a 200 carried "contract":2 (a 200 without it switches back); safety net until the entry reloads: a 422 on
        # a 0.20.0-shape post sends the 0.19.0 shape (hdev_off), a 413 the 0.20.0 keys uncompressed at 64/32 (caps_off)
        self.contract = self.answered = self.hdev_off = self.caps_off = self.trimmed = False
        # the entity ids of the last accepted 0.20.0-shape post, kept before new ones at the caps and the budget; per entry in hass.data, so they survive
        # a reload (the debounced one a new device entity triggers included); an HA restart starts afresh
        self.sents = hass.data.setdefault(DOMAIN, {}).setdefault("sent", {})
        self.order: list[str] = []  # the priority of this cycle's items (explicitly ticked first), for encode's trim
        # The contract is stored (load()): else every reload and restart would post one cycle without the whole devices, and SunnyHome ends the
        # bindings of entities that leave the snapshot. saved: what the store holds; unconfirmed: the contract came from the store and no 200
        # confirmed it in this run yet.
        self.store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.contract")
        self.saved = self.unconfirmed = False
        self.delay = 0
        self.retry_at = 0.0

    async def load(self) -> None:
        """HA-021: starts from the stored contract (a damaged store is none)."""
        data = await self.store.async_load()
        self.contract = self.saved = self.unconfirmed = isinstance(data, dict) and data.get("contract") == 2

    async def remember(self, contract: bool) -> None:
        """HA-021: stores the contract (only when it changes: a 200 without it, or a refused stored one, removes it)."""
        if contract != self.saved:
            await (self.store.async_save({"contract": 2}) if contract else self.store.async_remove())
        self.saved = contract

    @property
    def sent(self) -> frozenset[str]:
        return self.sents.get(self.entry.entry_id, frozenset())

    @property
    def v2(self) -> bool:
        """HA-021: whether the next post takes the 0.20.0 shape."""
        return self.contract and not self.hdev_off

    async def tick(self, _now: datetime) -> None:
        # HA-021: not before Home Assistant runs: an entity without a state yet would be left out, and SunnyHome ends the bindings of entities that
        # leave the snapshot (whole devices widen that a lot)
        if self.busy or self.stopped or time.monotonic() < self.retry_at or self.hass.state is not CoreState.running:
            return
        v2 = self.v2
        body = self.collect(v2)
        self.busy = True
        try:
            await self.post(body, v2)
        finally:
            self.busy = False
        self.issues()

    def issues(self) -> None:
        """HA-021: the repair issues of whole devices: SunnyHome is older (the 0.19.0 shape is posted, so nothing of them is sent) and a device past
        the caps or the byte budget (the first ones by entity id are sent)."""
        devices = bool(self.entry.options.get(CONF_DEVICES))
        for key, raised in ((ISSUE_OLD, devices and self.answered and not self.v2), (ISSUE_TRIM, devices and self.trimmed)):
            if raised:
                ir.async_create_issue(self.hass, DOMAIN, f"{key}_{self.entry.entry_id}", is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key=key)
            else:
                ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.entry.entry_id}")

    def collect(self, v2: bool = False) -> dict:
        """Body with the shared devices; "solax" is null unless exactly one inverter integration entry exists (GW-002: a goodwe
        entry posts "inverter" in its place; DEYE-003: so does a Deye entry with a valid serial, else "solax" stays null).
        HA-021: v2 is the 0.20.0 shape: the whole devices, the new keys and the 512 caps (64/32 after a 413)."""
        entries = inverter_entries(self.hass)
        if len(entries) > 1:
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key=ISSUE
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE)
        domain, hub = entries[0] if len(entries) == 1 else (SOLAX, None)
        solax = hub is not None and domain == SOLAX
        if hub is None:
            serial, readings, info = None, None, None
        elif solax:
            serial, readings, info = self.solax(hub)
        else:
            serial, readings, info = (*(self.deye(hub) if domain == SOLARMAN else self.goodwe(hub)), None)
        # SMB-004: the remote report for a SolaX plugin's hub only; SMB-007: settings also for a Growatt, Solis or Sofar hub (read only)
        profile = payload.settings_integration(info["plugin"]) if solax else None
        solax = solax and payload.solax_plugin(info["plugin"])
        limits = (payload.MAX_DEVICE_ENTITIES, payload.MAX_DEVICE_SENSORS) if v2 and not self.caps_off else (payload.MAX_ENTITIES, payload.MAX_SENSORS)
        # HA-021: at the caps a new entity never pushes out one already sent (whose binding SunnyHome would end): sent ones first (a stable sort keeps
        # the entity id order within each group), after the explicitly ticked ones
        devices = sorted(device_entities(self.hass, self.entry), key=lambda ent: ent.entity_id not in self.sent) if v2 else []
        explicit = {eid for key in (CONF_ENTITIES, CONF_BINARY, CONF_PEOPLE, CONF_HEAT, CONF_SENSORS) for eid in self.entry.options.get(key, [])}
        self.trimmed = False
        body = payload.build(
            dt_util.utcnow(), serial, readings, self.entities(v2, devices, limits[0]), controllable(self.hass, self.entry),
            self.sensors(v2, devices, limits[1]), self.remote(readings) if solax else None, self.settings(hub, profile) if profile else None, domain, info,
            self.wallbox(), None if self.wallboxes_off else self.other_wallboxes(), v2, limits,
        )
        rank = {ent.entity_id: -1 if ent.entity_id in explicit else n for n, ent in enumerate(devices)}  # a ticked entity is never trimmed before a device's
        self.order = sorted((item["entityId"] for key in ("entities", "sensors") for item in body.get(key) or ()), key=lambda eid: rank.get(eid, -1))
        return body

    def wallbox(self) -> dict | None:
        """EV-004: the EV-charger hub's envelope (readings, the work mode select, the control report), or None without one hub.
        Pre review P1-1: the work mode is left out while the select still shows SunnyHome's own write (no real poll since)."""
        hub = charger_hub(self.hass)
        if hub is None:
            return None
        prefix = f"{hub.options.get('name') or hub.data.get('name')}_"
        device = inverter_device(self.hass, hub)
        echoes = self.hass.data.setdefault(DOMAIN, {}).setdefault("echoes", {})
        readings = []
        for ent in er.async_entries_for_config_entry(er.async_get(self.hass), hub.entry_id):
            if ent.platform != SOLAX or ent.disabled or not ent.unique_id.startswith(prefix):
                continue
            key = ent.unique_id[len(prefix):]
            wanted = ent.domain == "sensor" and key in payload.WALLBOX_KEYS or ent.domain == "select" and key == payload.WALLBOX_MODE_KEY
            if not wanted or (state := self.hass.states.get(ent.entity_id)) is None:
                continue
            if ent.domain == "select" and (echo := echoes.get(ent.entity_id)) is not None and state.last_reported is not None \
                    and state.last_reported.timestamp() <= echo:
                continue
            readings.append((key, state.attributes.get("unit_of_measurement"), state.state, state.last_reported))
        charger = solax_charger(self.hass)
        option = bool(self.entry.options.get(CONF_WALLBOX))
        reason = "option_off" if not option else "entities_missing" if charger is None else "scan_interval" \
            if charger.scan_s > payload.WALLBOX_SCAN_MAX_S else None
        try:
            plugin = loader.async_get_loaded_integration(self.hass, SOLAX).version if reason is None else None
        except Exception:  # noqa: BLE001 - not loaded: no plugin version, so never verified
            plugin = None
        control = payload.wallbox_control(reason is None, reason, plugin, device.model if device else None, device.sw_version if device else None)
        return payload.wallbox_report(device.serial_number if device else None, readings, control)

    def other_wallboxes(self) -> list[dict] | None:
        """WALLBOX-005: the Other wallbox's item (power, session energy, status, the switch state and the number's own state as "current", and the control report),
        or None unless it is set up. The current is left out while the number still shows SunnyHome's own write (no real poll since)."""
        wallbox = other_wallbox(self.entry)
        if wallbox is None:
            return None
        echoes = self.hass.data.setdefault(DOMAIN, {}).setdefault("echoes", {})
        readings = []
        for key, entity_id in (("power", wallbox.power), ("energy", wallbox.energy), ("current", wallbox.number), ("status", wallbox.status), ("switch", wallbox.switch)):
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.last_reported is None:
                continue
            if key == "current" and (echo := echoes.get(entity_id)) is not None and state.last_reported.timestamp() <= echo:
                continue
            readings.append((key, "A" if key == "current" else state.attributes.get("unit_of_measurement"), state.state, state.last_reported))
        cfg = generic_config(self.hass, self.entry)
        entry = er.async_get(self.hass).async_get(wallbox.number)
        rng = (cfg.min_a, cfg.max_a, cfg.allows_zero) if cfg and cfg.min_a is not None else None
        control = payload.generic_control(bool(self.entry.options.get(CONF_OTHER_WALLBOX)), cfg is not None, rng,
                                          getattr(entry, "platform", None), wallbox.switch is not None)
        return [payload.generic_report(wallbox.number, wallbox.switch, readings, control)]

    def settings(self, hub: ConfigEntry, profile: str = SOLAX) -> dict | None:
        """ISET-004: the states of the hub's entities its integration's profile names, as "<domain>.<unique id key>"; read only.
        SMB-007: profile is the hub's settings profile (solax_modbus, or a brand's such as solax_modbus_growatt)."""
        prefix = f"{hub.options.get('name') or hub.data.get('name')}_"
        states = {}
        for ent in er.async_entries_for_config_entry(er.async_get(self.hass), hub.entry_id):
            if ent.platform != SOLAX or ent.disabled or not ent.unique_id.startswith(prefix):
                continue
            if (state := self.hass.states.get(ent.entity_id)) is not None:
                states[f"{ent.domain}.{ent.unique_id[len(prefix):]}"] = state.state
        return payload.settings(profile, states)

    def remote(self, readings: list | None) -> dict | None:
        """BAT-008 + BAT-003C: the inverter's remote mode whenever the hub has the mode sensor (option on or off), timed by the hub's
        snapshot (the newest last_reported of the profile readings and the mode sensor: solax_modbus writes a state only when it
        changes); control and the verified-key inputs only while the option is on and all four entities are there."""
        found, device_id = solax_entities(self.hass)
        if "sensor" not in found:
            return None
        state = self.hass.states.get(found["sensor"])
        times = [reading[3] for reading in readings or () if reading[3]] + ([state.last_reported] if state and state.last_reported else [])
        control = bool(self.entry.options.get(CONF_INVERTER)) and len(found) == len(REMOTE_KEYS)
        if not control:
            return payload.remote_report(False, state.state if state else None, max(times, default=None), None, None, None)
        device = main_device(self.hass, device_id)
        try:
            plugin = loader.async_get_loaded_integration(self.hass, SOLAX).version
        except Exception:  # noqa: BLE001 - not loaded: no plugin version, so never verified
            plugin = None
        return payload.remote_report(
            True, state.state if state else None, max(times, default=None), plugin,
            device.model if device else None, device.sw_version if device else None,
        )

    def device_of(self, ent: er.RegistryEntry, v2: bool = False) -> tuple[str | None, str | None, str | None, str | None]:
        """HA-016: the name (the user's rename first) and manufacturer of the entity's HA device; (None, None) without one. HA-021: and, for the
        0.20.0 shape only, its registry id and model."""
        device = dr.async_get(self.hass).async_get(ent.device_id) if ent.device_id and (v2 or not self.devices_off) else None
        if device is None:
            return None, None, None, None
        main = main_device(self.hass, ent.device_id) or device  # a child device's maker and model are its parent's (deprecated on the child)
        named = (device.name_by_user or device.name, main.manufacturer) if not self.devices_off else (None, None)
        return (*named, *((ent.device_id, main.model) if v2 else (None, None)))

    def entity_of(self, ent: er.RegistryEntry, state, v2: bool) -> payload.Entity:
        """One shared entity as payload reads it, by domain; HA-021: a config entity's category and the new climate attributes only for the 0.20.0 shape."""
        eid, domain = ent.entity_id, ent.domain
        device = self.device_of(ent, v2) if domain != "person" else (None, None, None, None)  # HA-016: a person has no device (home/away only)
        category = "config" if v2 and ent.entity_category == "config" else None
        if domain in SHARED_DOMAINS:
            modes = state.attributes.get("supported_color_modes")
            return payload.Entity(eid, state.name, state.state, state.attributes.get("brightness"), domain == "light" and brightness_supported(modes),
                                  state.last_changed, None, None, None, *device, category)
        if domain in ("binary_sensor", "person"):  # HA-012: only the state, never an attribute (a person has GPS and a zone)
            return payload.Entity(eid, state.name, state.state, None, False, state.last_changed,
                                  ent.device_class or ent.original_device_class if domain == "binary_sensor" else None, None, None, *device, category)
        if domain in payload.HEAT_DOMAINS:  # HA-013: the unit is the instance's (climate entities carry none); lastChanged is last_reported, the reading's time
            # HA-017: after a 422 of the fan and vane pairs their lists are left out, so heat() sends none (commands still read them, seen_of);
            # HA-021: the 0.19.0 shape leaves out the humidity, presets and range targets the same way
            dropped = (payload.AIR_LISTS if self.air_off else frozenset()) | (frozenset() if v2 else payload.V2_ATTRS)
            attrs = {k: v for k, v in state.attributes.items() if k not in dropped} if dropped else state.attributes
            return payload.Entity(eid, state.name, state.state, None, False, state.last_reported, None, attrs, self.hass.config.units.temperature_unit,
                                  *device, category)
        # HA-021: a select or number (whole devices only)
        return payload.Entity(eid, state.name, state.state, None, False, state.last_changed, None, state.attributes,
                              state.attributes.get("unit_of_measurement"), *device, category)

    def entities(self, v2: bool = False, devices: list | None = None, cap: int = payload.MAX_ENTITIES) -> list[payload.Entity]:
        """The ticked devices that are still shareable and in the state machine. HA-021: then, for the 0.20.0 shape, the other entities of the whole
        devices by entity id (an entity also ticked is sent once), up to cap: a device past it sends the first ones by entity id (trimmed)."""
        registry = er.async_get(self.hass)
        items, seen, excluded = [], set(), unshared_entries(self.hass)

        def add(ent: er.RegistryEntry | None, rule) -> None:
            if ent is None or ent.entity_id in seen or not rule(ent) or (state := self.hass.states.get(ent.entity_id)) is None:
                return
            seen.add(ent.entity_id)
            items.append(self.entity_of(ent, state, v2))
        for entity_id in self.entry.options.get(CONF_ENTITIES, [])[: payload.MAX_ENTITIES]:
            add(registry.async_get(entity_id), lambda ent: shareable(ent, excluded))
        if not self.states_off:  # HA-012: binary sensors, then people
            for key, rule in ((CONF_BINARY, lambda ent: binary_shareable(ent, excluded)), (CONF_PEOPLE, person_shareable)):
                for entity_id in self.entry.options.get(key, [])[: payload.MAX_ENTITIES]:
                    add(registry.async_get(entity_id), rule)
        if not self.heat_off:  # HA-013: heat pumps and boilers last
            for entity_id in self.entry.options.get(CONF_HEAT, [])[: payload.MAX_ENTITIES]:
                add(registry.async_get(entity_id), lambda ent: heat_shareable(ent, excluded))
        items = items[: payload.MAX_ENTITIES]
        skipped = {"sensor"} | ({"binary_sensor"} if self.states_off else set()) | (set(payload.HEAT_DOMAINS) if self.heat_off else set())
        for ent in devices or ():  # HA-021: whole_device applied the rules already
            if ent.domain not in skipped:
                add(ent, lambda ent: True)
        self.trimmed |= len(items) > cap
        return items[:cap]

    def sensors(self, v2: bool = False, devices: list | None = None, cap: int = payload.MAX_SENSORS) -> list[payload.Sensor] | None:
        """The ticked sensors still shareable and in the state machine; None (no "sensors" key) while none is ticked (HA-005).
        HA-021: then, for the 0.20.0 shape, the numeric sensors of the whole devices (any class, D3), their state class and Energy dashboard role."""
        ticked = self.entry.options.get(CONF_SENSORS, [])
        extra = [ent for ent in devices or () if ent.domain == "sensor"]
        if not ticked and not extra:
            return None
        registry = er.async_get(self.hass)
        items, seen, excluded, roles = [], set(), unshared_entries(self.hass), energy_roles(self.hass) if v2 else {}
        picked = [(registry.async_get(entity_id), lambda ent: sensor_shareable(ent, excluded)) for entity_id in ticked[: payload.MAX_SENSORS]]
        for ent, rule in picked + [(ent, lambda ent: True) for ent in extra]:  # whole_device applied the rules already
            if ent is None or ent.entity_id in seen or not rule(ent) or (state := self.hass.states.get(ent.entity_id)) is None:
                continue
            seen.add(ent.entity_id)
            items.append(payload.Sensor(
                ent.entity_id, state.name, ent.device_class or ent.original_device_class, state.state,
                state.attributes.get("unit_of_measurement"), state.last_reported, *self.device_of(ent, v2),
                state.attributes.get("state_class") if v2 else None, roles.get(ent.entity_id),
            ))
        self.trimmed |= len(items) > cap
        return items[:cap]

    def solax(self, hub: ConfigEntry) -> tuple[str | None, list, dict]:
        """(serial, readings, plugin and manufacturer) of the profile sensors of the single solax_modbus hub. HA-008: the serial and
        manufacturer are the hub's inverter device's (for SolaX the device of a SERIAL_KEYS sensor when it is not found). SMB-004: a
        Growatt, Solis or Sofar hub sends its brand's keys; Growatt's serial is its serialnumber sensor and the device's serial number
        goes as "family" (it is often the firmware string)."""
        prefix = f"{hub.options.get('name') or hub.data.get('name')}_"
        devices = dr.async_get(self.hass)
        inverter = inverter_device(self.hass, hub)
        device_serial = inverter.serial_number if inverter else None
        info = payload.hub(hub_plugin(hub), inverter.manufacturer if inverter else None, device_serial)
        plugin = info["plugin"]
        keys, brand = payload.hub_keys(plugin), plugin in payload.BRAND_KEYS
        serial = None if plugin == "growatt" else payload.ident(device_serial) if brand else device_serial
        readings = []
        registry = er.async_get(self.hass)
        for ent in er.async_entries_for_config_entry(registry, hub.entry_id):
            if ent.domain != "sensor" or ent.platform != SOLAX or not ent.unique_id.startswith(prefix):
                continue
            key = ent.unique_id[len(prefix):]
            if payload.auto_enable(key, ent.disabled_by, plugin):
                # SRC-009: HA reloads the hub entry by itself (about 30 s) once an integration-disabled entity is enabled
                registry.async_update_entity(ent.entity_id, disabled_by=None)
                _LOGGER.info("Enabled %s so SunnyHome can read this inverter's data in full", ent.entity_id)
                continue
            if ent.disabled:
                continue
            if plugin == "growatt" and key == "serialnumber":
                serial = payload.ident(state.state) if (state := self.hass.states.get(ent.entity_id)) is not None else None
                continue
            if key not in keys:
                continue
            if serial is None and not brand and key in SERIAL_KEYS and ent.device_id and (device := main_device(self.hass, ent.device_id)):
                serial = device.serial_number
            if (state := self.hass.states.get(ent.entity_id)) is not None:
                readings.append((key, state.attributes.get("unit_of_measurement"), state.state, state.last_reported))
        return serial, readings, info

    def goodwe(self, entry: ConfigEntry) -> tuple[str | None, list]:
        """GW-002: (serial, readings) of the profile sensors of the single goodwe entry. The serial is only in the device registry
        identifiers ("goodwe", <serial>) (the device's serial_number is empty); the key is the <id> of unique_id goodwe-<id>-<serial>."""
        devices = dr.async_get(self.hass)
        serial = None
        readings = []
        for ent in er.async_entries_for_config_entry(er.async_get(self.hass), entry.entry_id):
            if ent.domain != "sensor" or ent.platform != GOODWE or ent.disabled or not ent.device_id:
                continue
            device = devices.async_get(ent.device_id)
            ident = next((i[1] for i in device.identifiers if len(i) == 2 and i[0] == GOODWE), None) if device else None
            if ident is None or (serial is not None and ident != serial):
                continue
            if (key := payload.goodwe_key(ent.unique_id, ident)) not in payload.GOODWE_KEYS:
                continue
            serial = ident
            if (state := self.hass.states.get(ent.entity_id)) is not None:
                readings.append((key, state.attributes.get("unit_of_measurement"), state.state, state.last_reported))
        return serial, readings

    def deye(self, entry: ConfigEntry) -> tuple[str | None, list | None]:
        """DEYE-003: (serial, readings) of the Deye sensors of the single Deye solarman entry, or (None, None) (then "solax": null)
        while the Device sensor or its Serial Number attribute is missing or invalid: the registry serial_number falls back to the
        logger serial, so it is never used. The key is the sensor unique_id <entry id>_<key>_sensor minus prefix and suffix."""
        ents = [ent for ent in er.async_entries_for_config_entry(er.async_get(self.hass), entry.entry_id)
                if ent.domain == "sensor" and ent.platform == SOLARMAN and not ent.disabled]
        device = next((ent for ent in ents if payload.deye_key(ent.unique_id, entry.entry_id) == "device"), None)
        state = self.hass.states.get(device.entity_id) if device else None
        if (serial := payload.deye_serial(state.attributes.get("Serial Number") if state else None)) is None:
            return None, None
        readings = []
        for ent in ents:
            if (key := payload.deye_key(ent.unique_id, entry.entry_id)) in payload.DEYE_KEYS and \
                    (state := self.hass.states.get(ent.entity_id)) is not None:
                readings.append((key, state.attributes.get("unit_of_measurement"), state.state, state.last_reported))
        return serial, readings

    async def post(self, body: dict, v2: bool = False) -> None:
        """Never logs the token, payload values or device names; only counts. HA-021: a 0.20.0-shape post (v2) goes gzip-compressed (unless caps_off),
        with its own Content-Encoding and Content-Type and within the byte budget; every 200 tells whether SunnyHome takes that shape ("contract":2)."""
        status = None
        packed, dropped = v2 and not self.caps_off, frozenset()
        if packed:
            data, dropped = await self.hass.async_add_executor_job(payload.encode, body, self.order)  # json + gzip of up to ~2 MB: never in the loop
            self.trimmed |= bool(dropped)
            request = {"data": data, "headers": {**self.headers, "Content-Encoding": "gzip", "Content-Type": "application/json"}}
        else:
            request = {"json": body, "headers": self.headers}
        try:
            async with async_get_clientsession(self.hass).post(self.url, timeout=aiohttp.ClientTimeout(total=10), **request) as resp:
                status = resp.status
                if status == 200:
                    try:
                        answer = await resp.json(content_type=None)
                    except ValueError:
                        answer = None
                    self.answered, self.contract, self.unconfirmed = True, isinstance(answer, dict) and answer.get("contract") == 2, False
                    await self.remember(self.contract)
                    if not self.contract:  # an old (rolled back) server explains the refusals: a re-deploy's contract switches back without a reload
                        self.hdev_off = self.caps_off = False
                    elif v2:
                        self.sents[self.entry.entry_id] = frozenset(item["entityId"] for key in ("entities", "sensors") for item in body.get(key) or ()) - dropped
                    inverter = body.get("solax") or body.get("inverter") or {}
                    _LOGGER.debug(
                        "Posted %d readings, %d settings, %d devices and %d sensors (%s shape): %s",
                        len(inverter.get("readings", [])), len((inverter.get("settings") or {}).get("states", {})),
                        len(body["entities"]), len(body.get("sensors", [])), "0.20.0" if v2 else "0.19.0", str(answer)[:200],
                    )
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Post failed: %s", type(err).__name__)
        if status == 200:
            self.delay, self.retry_at = 0, 0.0
        elif status == 401:
            self.stopped = True
            _LOGGER.warning("SunnyHome refused the token; pair again with a new code")
            self.entry.async_start_reauth(self.hass)
        elif v2 and self.unconfirmed and status is not None and 400 <= status < 500 and status != 429:
            # HA-021: the stored contract was refused before a 200 confirmed it (a rolled back server: 422, 413, 415, 400 ...; 401 is the token's and
            # a 429 only a busy inflate permit): the 0.19.0 shape for the rest of this run, and the store is cleared so the next start asks again
            self.hdev_off, self.contract, self.unconfirmed = True, False, False
            await self.remember(False)
            _LOGGER.warning("SunnyHome rejected a post carrying whole devices (HTTP %s), probably because its server is older; "
                            "sending only the explicitly ticked entities until this integration is reloaded", status)
        elif status == 422 and v2:
            # HA-021: checked before the older flags: a server that answered "contract":2 but refuses the new shape (rolled back, or one bad item)
            self.hdev_off = True
            await self.remember(False)
            _LOGGER.warning("SunnyHome rejected a post carrying whole devices (HTTP 422), probably because its server is older (or another item was refused); "
                            "sending only the explicitly ticked entities until this integration is reloaded")
        elif status == 413 and v2 and not packed:
            # HA-021: still too large uncompressed at 64/32 (non-ASCII escaped, no budget): the 0.19.0 shape, which a 0.19.0 component posts too
            self.hdev_off = True
            await self.remember(False)
            _LOGGER.warning("SunnyHome rejected a post carrying whole devices as too large (HTTP 413); sending only the explicitly ticked entities "
                            "until this integration is reloaded")
        elif status == 413 and packed:
            self.caps_off = True
            _LOGGER.warning("SunnyHome rejected a compressed post as too large (HTTP 413); sending at most 64 devices and 32 sensors uncompressed "
                            "until this integration is reloaded")
        elif status == 422 and not self.air_off and any(key in e.get("heat", {}) for e in body["entities"] for key in ("fanModes", "swingModes", "swingHorizontalModes")):
            # HA-017: the newest item stops first, so a server before HA-017 (an older or rolled back one) stops only the fan and vane pairs.
            self.air_off = True
            _LOGGER.warning("SunnyHome rejected a post carrying fan and vane settings (HTTP 422), probably because its server is older (or another item was refused); "
                            "not sending them until this integration is reloaded")
        elif status == 422 and not self.devices_off and any("deviceName" in e for e in body["entities"] + body.get("sensors", [])):
            # HA-016: the newest item stops first, so a server before HA-016 (an older or rolled back one) stops only the device names.
            self.devices_off = True
            _LOGGER.warning("SunnyHome rejected a post carrying device names (HTTP 422), probably because its server is older (or another item was refused); "
                            "not sending them until this integration is reloaded")
        elif status == 422 and not self.wallboxes_off and "wallboxes" in body:
            # WALLBOX-005: checked before the heat items (the newest item stops first), so a refusal of the Other wallbox (an older server, or one bad
            # entity) stops only it and the whole post goes through again.
            self.wallboxes_off = True
            _LOGGER.warning("SunnyHome rejected a post carrying the other wallbox (HTTP 422), probably because its server is older (or another item was refused); "
                            "not sending it until this integration is reloaded")
        elif status == 422 and not self.heat_off and any("heat" in e for e in body["entities"]):
            # HA-013: checked first, so a refusal of the heat items (an older server, or one bad device) stops only them, not the binary sensors and people.
            self.heat_off = True
            _LOGGER.warning("SunnyHome rejected a post carrying heat pumps or boilers (HTTP 422), probably because its server is older (or another item was refused); "
                            "not sending them until this integration is reloaded")
        elif status == 422 and not self.states_off and any("kind" in e for e in body["entities"]):
            # HA-012: a server before 0.14.0 refuses the new items and with them the whole post, every cycle: stop sending them.
            self.states_off = True
            _LOGGER.warning("SunnyHome rejected a post carrying binary sensors and people (HTTP 422), probably because its server is older (or another item was refused); "
                            "not sending them until this integration is reloaded")
        elif status in (413, 422):
            if not self.warned:
                self.warned = True
                _LOGGER.warning("SunnyHome rejected the readings (HTTP %s); continuing", status)
        else:
            self.delay = min(self.delay * 2 or BACKOFF_MIN, BACKOFF_MAX)
            self.retry_at = time.monotonic() + self.delay
            _LOGGER.debug("Post failed (HTTP %s); next attempt in %s s", status, self.delay)


class CommandRunner:
    """Claims commands by long-poll and runs them through payload.execute; AUTO-004: keeps the entry's leases (one per entry)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.url = f"{entry.data[CONF_URL]}/api/ha/v1/commands"
        self.headers = {"Authorization": f"Bearer {entry.data[CONF_TOKEN]}"}
        # Shared by all entries and kept across reloads (async_unload_entry never removes it).
        shared = hass.data.setdefault(DOMAIN, {})
        shared.setdefault("locks", {})
        self.contexts = shared.setdefault("contexts", {})  # entity id -> the context of its newest SunnyHome service call
        self.store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.leases")
        self.leases = payload.Leases()
        self.restarted: set[str] = set()
        self.checked: dict[str, float] = {}  # HA-021 (§2g): when the lease check first saw each due lease (the give-up time counts from it)
        self.reports: list[tuple[dict, float]] = []
        self.ticking = False
        self.off_sent: dict[str, float] = {}  # entity id -> when the last unconfirmed lease off was sent (monotonic)
        # BAT-008: the inverter lease has its own store; the hub lock is shared by all entries and kept across reloads.
        self.inverter_store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.inverter")
        self.inverter = payload.Inverter(
            shared["locks"].setdefault(SOLAX, asyncio.Lock()), lambda: solax_remote(hass),
            lambda: bool(entry.options.get(CONF_INVERTER)), self.raw_state, self.dispatch, self.save_inverter,
            lambda command, outcome, reason: self.report(command, outcome, reason, None),
        )
        # EV-004: the wallbox lease has its own store and lock; the echoes (the select's last_reported of SunnyHome's own write) are shared
        # with the pusher, which withholds the work mode until a real poll.
        self.wallbox_store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.wallbox")
        self.wallbox = payload.Wallbox(
            shared["locks"].setdefault(CHARGER_LOCK, asyncio.Lock()), lambda: solax_charger(hass),
            lambda: bool(entry.options.get(CONF_WALLBOX)), self.select_state, self.dispatch, self.save_wallbox, shared.setdefault("echoes", {}),
        )
        # WALLBOX-005: the Other wallbox lease has its own store and lock too; it shares the echoes with the pusher (the number's own write)
        self.generic_store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.wallbox_generic")
        self.generic = payload.GenericWallbox(
            shared["locks"].setdefault(OTHER_WALLBOX_LOCK, asyncio.Lock()), lambda: generic_config(hass, entry),
            lambda: bool(entry.options.get(CONF_OTHER_WALLBOX)), self.select_state, self.dispatch, self.save_generic, shared.setdefault("echoes", {}),
        )
        self.env = payload.Env(
            shared["locks"], shared.setdefault("inflight", {}),
            lambda entity_id: entity_id in controllable(hass, entry), self.seen, self.dispatch,
            leases=self.leases, save=self.save, dispatch_many=self.dispatch_many,
        )

    async def start_leases(self) -> None:
        """Loads the stored leases (a lease that ended while HA was down is ended at the next check, reported as a restart)."""
        self.leases.items.update(payload.Leases(await self.store.async_load()).items)
        self.restarted = {eid for eid, _ in self.leases.due(time.time())}
        self.entry.async_on_unload(async_track_time_interval(self.hass, self.lease_tick, timedelta(seconds=LEASE_TICK_S), name="SunnyHome leases"))
        # HA's callback marks a function object; a bound method refuses the attribute (AttributeError), so wrap it.
        self.entry.async_on_unload(async_track_state_change_event(self.hass, watched(self.hass, self.entry),  # HA-021: with the whole devices' controllable entities
                                                                  callback(lambda event: self.state_changed(event))))
        self.entry.async_create_background_task(self.hass, self.send_reports(), "SunnyHome lease reports")
        # BAT-008: the inverter lease survives unload, reload and restart; the tick also ends it when the option was turned off.
        self.inverter.load(await self.inverter_store.async_load())
        self.entry.async_on_unload(lambda: setattr(self.inverter, "unloaded", True))
        self.entry.async_on_unload(async_track_time_interval(
            self.hass, self.inverter.tick, timedelta(seconds=LEASE_TICK_S), name="SunnyHome inverter lease"
        ))
        # EV-004: the wallbox lease survives unload, reload and restart; the tick also ends it when the option was turned off.
        self.wallbox.load(await self.wallbox_store.async_load())
        self.entry.async_on_unload(lambda: setattr(self.wallbox, "unloaded", True))
        self.entry.async_on_unload(async_track_time_interval(
            self.hass, self.wallbox.tick, timedelta(seconds=LEASE_TICK_S), name="SunnyHome wallbox lease"
        ))
        # WALLBOX-005: the Other wallbox lease survives unload, reload and restart; the tick also ends it when the option was turned off.
        self.generic.load(await self.generic_store.async_load())
        self.entry.async_on_unload(lambda: setattr(self.generic, "unloaded", True))
        self.entry.async_on_unload(async_track_time_interval(
            self.hass, self.generic.tick, timedelta(seconds=LEASE_TICK_S), name="SunnyHome other wallbox lease"
        ))

    async def save_inverter(self, data: dict | None) -> None:
        if data is None:
            await self.inverter_store.async_remove()
        else:
            await self.inverter_store.async_save(data)

    async def save_wallbox(self, data: dict | None) -> None:
        if data is None:
            await self.wallbox_store.async_remove()
        else:
            await self.wallbox_store.async_save(data)

    async def save_generic(self, data: dict | None) -> None:
        if data is None:
            await self.generic_store.async_remove()
        else:
            await self.generic_store.async_save(data)

    def select_state(self, entity_id: str) -> tuple[str | None, float | None] | None:
        """EV-004: a select's state and its last_reported (UTC seconds), or None; WALLBOX-005: also a number's or switch's."""
        if (state := self.hass.states.get(entity_id)) is None:
            return None
        return state.state, state.last_reported.timestamp() if state.last_reported else None

    def raw_state(self, entity_id: str) -> str | None:
        return state.state if (state := self.hass.states.get(entity_id)) is not None else None

    async def save(self) -> None:
        await self.store.async_save(self.leases.to_dict())

    async def lease_tick(self, _now: datetime | None = None) -> None:
        """Ends the due leases: an off through the command path (control list, lock, availability); the lease is dropped and
        lease_ended reported only once the device is seen off. An off that was not confirmed keeps the lease: the next check reads
        the state again and sends the off again at most once a minute, for at most a day after the lease end."""
        if self.ticking:
            return
        self.ticking = True
        try:
            now = time.time()
            for eid, lease in self.leases.due(now):
                if eid not in controllable(self.hass, self.entry):  # the user took the device out of SunnyHome's hands: left as it is
                    await self._drop(eid, lease, "lease_cancelled", "not_controllable")
                    continue
                # HA-021 (§2g): counted from the later of the lease end and the first check that saw it due, so a lease that ended during a long HA outage
                # still gets its restore (or off), reported as restart
                start = max(lease.until, self.checked.setdefault(eid, now))
                if now > start + (payload.HEAT_RESTORE_GIVE_UP_S if lease.target == "restore" else LEASE_GIVE_UP_S):  # ponytail: a device not seen off for a day is given up
                    await self._drop(eid, lease, "lease_cancelled", None)
                    continue
                if lease.target == "restore":  # HA-014: before the "off" test below: for a climate "off" is a mode, not the lease's end
                    await self._restore_due(eid, lease)
                    continue
                seen = self.seen(eid)
                if seen is None or seen.state in ("unavailable", "unknown"):
                    continue  # retried at the next check
                if seen.state == "off":
                    await self._drop(eid, lease, "lease_ended", "restart" if eid in self.restarted else None)
                    continue
                if time.monotonic() < self.off_sent.get(eid, -LEASE_OFF_RETRY_S) + LEASE_OFF_RETRY_S:
                    continue  # an off is on its way or was just not confirmed: wait for the readback
                off = payload.Command(str(uuid.uuid4()), eid, "off", None, payload.TTL_MAX_MS)
                outcome = (await payload.execute(off, time.monotonic() + payload.TTL_MAX_MS / 1000, self.env))[0]
                if outcome == "done":
                    await self._drop(eid, lease, "lease_ended", "restart" if eid in self.restarted else None)
                else:  # execute ended the lease before its checks: put it back until the device is seen off
                    self.leases.items[eid] = lease
                    await self.save()
                    self.off_sent[eid] = time.monotonic()
        finally:
            self.ticking = False

    async def _restore_due(self, eid: str, lease: payload.Lease) -> None:
        """HA-014: a restore lease that ended: the previous target and mode go back where the entity still shows what the lease set (ended
        when it shows the previous values already); anything else was changed by hand (cancelled, nothing restored). A restore that is not
        confirmed keeps the lease; it is tried again after a minute, for at most an hour (HEAT_RESTORE_GIVE_UP_S)."""
        seen = self.seen(eid)
        if seen is None or seen.state in ("unavailable", "unknown"):
            return  # retried at the next check
        if time.monotonic() < self.off_sent.get(eid, -LEASE_OFF_RETRY_S) + LEASE_OFF_RETRY_S:
            return
        outcome, reason, _ = await payload.restore(eid, lease, self.env)
        if outcome == "done":
            await self._drop(eid, lease, "lease_ended", "restart" if eid in self.restarted else None)
        elif reason == "lease_changed":
            return  # PLAN-031 integration: the lease was replaced while this check waited: the new one is not ours to drop
        elif reason in ("outside_change", "restore_invalid"):  # HA-021 (§2g): a previous value that no longer validates is never written
            await self._drop(eid, lease, "lease_cancelled", reason)
        elif eid in self.leases.items:
            self.off_sent[eid] = time.monotonic()

    async def _drop(self, eid: str, lease: payload.Lease, outcome: str, reason: str | None) -> None:
        self.leases.cancel(eid)
        await self.save()
        self.off_sent.pop(eid, None)
        self.restarted.discard(eid)
        self.checked.pop(eid, None)
        self.report(lease.command, outcome, reason)

    def state_changed(self, event) -> None:
        """A change that leaves a lease's target and is not this component's own call ends the lease (reported as outside_change)."""
        data = event.data
        eid, old, new = data.get("entity_id"), data.get("old_state"), data.get("new_state")
        lease = self.leases.items.get(eid)
        if lease is None or old is None or new is None or new.state in ("unavailable", "unknown"):
            return  # no lease, HA starting or the entity gone: not a change by anyone
        if lease.target == "restore":  # HA-014: before the "off" test below: a climate's "off" is a mode
            self.heat_changed(eid, lease, new)
            return
        if new.state == "off" and lease.until <= time.time():
            # The lease's own off, confirmed late (perhaps under another context): the lease ends here, so a later hand "on" finds
            # no lease and is never undone by a second off (post R2). During a lease off execute holds no lease, so this never doubles.
            self.leases.cancel(eid)
            self.off_sent.pop(eid, None)
            self.hass.async_create_task(self.save(), "SunnyHome lease save")
            self.report(lease.command, "lease_ended", "restart" if eid in self.restarted else None)
            self.restarted.discard(eid)
            return
        attrs = new.attributes
        seen = payload.Seen(new.state, attrs.get("brightness"), new.domain == "light" and brightness_supported(attrs.get("supported_color_modes")), False)
        if payload.at_target(lease, seen):
            return  # a confirmation (often under another context) that keeps the target
        ours = self.contexts.get(eid)
        if ours is not None and ours in (new.context.id, new.context.parent_id):
            return
        self.leases.cancel(eid)
        self.hass.async_create_task(self.save(), "SunnyHome lease save")
        self.report(lease.command, "lease_cancelled", "outside_change")

    def heat_changed(self, eid: str, lease: payload.Lease, new) -> None:
        """HA-014: a change that leaves what a restore lease set, and is not SunnyHome's own, ends the lease without a restore (outside_change).
        While a command or a restore runs on the entity (its lock is held) nothing counts; the lease check sees what is left."""
        seen = self.seen_of(new)
        if payload.at_target(lease, seen):
            return  # a confirmation, or an attribute that is not the lease's (the current temperature, the action)
        if (lock := self.env.locks.get(eid)) is not None and lock.locked():
            return
        ours = self.contexts.get(eid)
        if ours is not None and ours in (new.context.id, new.context.parent_id):
            return
        if lease.until <= time.time() and payload.heat_verdict(lease, seen)[0] == "ended":
            # PLAN-031 integration: the restore, confirmed late (perhaps under another context): the lease ends here, as a switch's late off does.
            self.leases.cancel(eid)
            self.off_sent.pop(eid, None)
            self.hass.async_create_task(self.save(), "SunnyHome lease save")
            self.report(lease.command, "lease_ended", "restart" if eid in self.restarted else None)
            self.restarted.discard(eid)
            return
        self.leases.cancel(eid)
        self.hass.async_create_task(self.save(), "SunnyHome lease save")
        self.report(lease.command, "lease_cancelled", "outside_change")

    def report(self, command: str, outcome: str, reason: str | None, assumed: bool | None = False) -> None:
        self.reports.append(({"commandId": command, "outcome": outcome, "reason": reason, "assumedState": assumed}, time.monotonic()))

    async def send_reports(self) -> None:
        """At most one report a second (under the result budget), retried with a backoff for up to 15 minutes, then dropped."""
        delay = 1
        while True:
            if not self.reports:
                await asyncio.sleep(1)
                continue
            body, first = self.reports[0]
            status = (await self.post("result", body, 10))[0]
            if status in (200, 401, 404, 422) or time.monotonic() > first + REPORT_WINDOW_S:
                self.reports.pop(0)
                delay = 1
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def run(self) -> None:
        delay = 0
        while True:
            # ttlMs counts from before the claim was sent, so a slow answer only shortens it and a command never
            # runs after its server-side expiry (RV-19).
            sent = time.monotonic()
            status, body = await self.post("claim", {}, 35)
            commands = body.get("commands") if isinstance(body, dict) else None
            if status == 200 and isinstance(commands, list):
                delay = 0
                for item in commands:
                    inverter = isinstance(item, dict) and "inverter" in item  # BAT-008
                    generic = isinstance(item, dict) and item.get("wallbox") == "set_current"  # WALLBOX-005
                    wallbox = isinstance(item, dict) and "wallbox" in item and not generic  # EV-004
                    parse = payload.parse_generic if generic else payload.parse_wallbox if wallbox else payload.parse_inverter if inverter else payload.parse_command
                    run = self.execute_generic if generic else self.execute_wallbox if wallbox else self.execute_inverter if inverter else self.execute
                    cid, cmd = parse(item)
                    if cid is not None:  # an invalid command without a valid id is dropped
                        self.entry.async_create_background_task(self.hass, run(cid, cmd, sent), "SunnyHome command")
                if not commands:
                    await asyncio.sleep(2)
            elif status == 401:
                _LOGGER.warning("SunnyHome refused the token; pair again with a new code")
                self.entry.async_start_reauth(self.hass)
                return
            elif status == 404:
                await asyncio.sleep(3600)  # control is off on the server
            else:
                delay = min(delay * 2 or BACKOFF_MIN, BACKOFF_MAX)
                _LOGGER.debug("Claim failed (HTTP %s); next attempt in %s s", status, delay)
                await asyncio.sleep(delay)

    async def execute(self, cid: str, cmd: payload.Command | None, sent: float) -> None:
        if cmd is None:
            until, (outcome, reason, assumed) = sent, ("failed", "invalid_command", False)
        else:
            until = payload.deadline(sent, cmd.ttl_ms)
            outcome, reason, assumed = await payload.execute(cmd, until, self.env)
        await self.result({"commandId": cid, "outcome": outcome, "reason": reason, "assumedState": assumed}, until)

    async def execute_inverter(self, cid: str, cmd: payload.InverterCommand | None, sent: float) -> None:
        """BAT-008: one inverter command; its result carries assumedState null."""
        outcome, reason = ("failed", "invalid_command") if cmd is None else await self.inverter.execute(cmd, sent)
        await self.result({"commandId": cid, "outcome": outcome, "reason": reason, "assumedState": None}, time.monotonic())

    async def execute_wallbox(self, cid: str, cmd: payload.WallboxCommand | None, sent: float) -> None:
        """EV-004: one wallbox command; its result carries assumedState null. P3-2: no lease reports for wallbox rows."""
        outcome, reason = ("failed", "invalid_command") if cmd is None else await self.wallbox.execute(cmd, sent)
        await self.result({"commandId": cid, "outcome": outcome, "reason": reason, "assumedState": None}, time.monotonic())

    async def execute_generic(self, cid: str, cmd: payload.GenericCommand | None, sent: float) -> None:
        """WALLBOX-005: one set_current command (an apply, a renewal or a restore); its result carries assumedState null, like a wallbox command."""
        outcome, reason = ("failed", "invalid_command") if cmd is None else await self.generic.execute(cmd, sent)
        await self.result({"commandId": cid, "outcome": outcome, "reason": reason, "assumedState": None}, time.monotonic())

    async def result(self, body: dict, until: float) -> None:
        delay = 1
        # The server takes the result once and answers 200 to repeats; retry until 30 s past the deadline.
        while (await self.post("result", body, 10))[0] not in (200, 401, 404, 422) and time.monotonic() < until + 30:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)

    def seen(self, entity_id: str) -> payload.Seen | None:
        if (state := self.hass.states.get(entity_id)) is None:
            return None
        return self.seen_of(state)

    def seen_of(self, state) -> payload.Seen:
        attrs = state.attributes
        # HA-014: a heat entity carries heat() of its state: the target, mode, range, step and modes the component reports; HA-021: a select or number setting()
        heat = (payload.heat(state.domain, state.state, attrs, self.hass.config.units.temperature_unit) if state.domain in payload.HEAT_DOMAINS
                else payload.setting(state.domain, state.state, attrs) if state.domain in payload.SETTING_DOMAINS else None)
        return payload.Seen(
            state.state, attrs.get("brightness"),
            state.domain == "light" and brightness_supported(attrs.get("supported_color_modes")),
            attrs.get("assumed_state") is True, heat,
        )

    def dispatch(self, domain: str, service: str, data: dict) -> asyncio.Task:
        return self.start([(domain, service, data)])

    def dispatch_many(self, calls: list) -> asyncio.Task:
        """HA-014: several service calls of one command in one task and one context, in order; a call that raises ends the task (the rest is not sent)."""
        return self.start(calls)

    def start(self, calls: list) -> asyncio.Task:
        domain, service, data = calls[0]
        # AUTO-004: its own context, so a lease does not take this call's state change for one from another context.
        context = Context()
        self.contexts[data["entity_id"]] = context.id

        async def run() -> None:
            for call_domain, call_service, call_data in calls:
                await self.hass.services.async_call(call_domain, call_service, call_data, blocking=True, context=context)
        # A hass task, never cancelled (not even on unload): the entity stays busy until the call ends.
        task = self.hass.async_create_task(
            self.hass.services.async_call(domain, service, data, blocking=True, context=context) if len(calls) == 1 else run(),
            "SunnyHome service call", eager_start=True
        )
        # async_call raises these before the service runs, so the eager start has already finished the task:
        # nothing was switched (failed). Any later error, ServiceValidationError included, ends unknown.
        if task.done() and not task.cancelled() and isinstance(err := task.exception(), (ServiceNotFound, vol.Invalid)):
            raise err
        return task

    async def post(self, path: str, body: dict, timeout: int) -> tuple[int | None, object]:
        """(status, JSON of a 200 answer). Never logs the token or the commands."""
        # AUTO-004: a claim announces lease support (a capability, not authority; an older SunnyHome ignores the header).
        headers = {**self.headers, "X-SunnyHome-Leases": "1"} if path == "claim" else self.headers
        if path == "claim":  # RA-004: from 0.6.0 SunnyHome also sends no_discharge (mode 7); HA-014: from 0.16.0 heat commands; WALLBOX-005: from 0.17.0 set_current; so on every claim
            headers["X-SunnyHome-Component"] = VERSION
        if path == "claim" and self.entry.options.get(CONF_INVERTER):  # BAT-008: inverter commands only while the option is on
            headers["X-SunnyHome-Inverter"] = "1"
        if path == "claim" and (self.entry.options.get(CONF_WALLBOX) or self.entry.options.get(CONF_OTHER_WALLBOX)):  # EV-004, WALLBOX-005: wallbox commands only while an option is on
            headers["X-SunnyHome-Wallbox"] = "1"
        try:
            async with async_get_clientsession(self.hass).post(
                f"{self.url}/{path}", json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                return resp.status, await resp.json(content_type=None) if resp.status == 200 else None
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Command %s failed: %s", path, type(err).__name__)
            return None, None

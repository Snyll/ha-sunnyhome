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
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

import aiohttp
import voluptuous as vol

from homeassistant import loader
from homeassistant.components.light import brightness_supported
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_URL
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import storage
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from . import payload
from .const import CONF_CONTROL, CONF_ENTITIES, CONF_INVERTER, CONF_SENSORS, DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)
SOLAX = "solax_modbus"
GOODWE = "goodwe"  # GW-002
INVERTERS = (SOLAX, GOODWE)  # the inverter integrations: exactly one entry across them is read, their entities are never shared
SOLARMAN = "solarman"  # DEYE-003: ha-solarman (Deye) and HA core's Solarman plugs share this domain; only Deye entries count
ISSUE = "multiple_hubs"  # GW-002: several inverter integration entries (of one kind or mixed)
SERIAL_KEYS = ("measured_power", "inverter_power")
SHARED_DOMAINS = ("switch", "light")
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


def inverter_entries(hass: HomeAssistant) -> list[tuple[str, ConfigEntry]]:
    """GW-002: the (domain, entry) of every inverter integration entry (solax_modbus, goodwe; DEYE-003: Deye solarman entries)."""
    found = [(domain, entry) for domain in INVERTERS for entry in hass.config_entries.async_entries(domain)]
    return found + [(SOLARMAN, entry) for entry in deye_entries(hass)]


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
    """The allow-list rule for picked sensors (HA-005): an enabled temperature, power or energy sensor, not of an inverter integration."""
    return (
        ent.domain == "sensor" and ent.platform not in INVERTERS and not ent.disabled
        and not (excluded and ent.config_entry_id in excluded)
        and (ent.device_class or ent.original_device_class) in payload.SENSOR_CLASSES
        and payload.SENSOR_ID.fullmatch(ent.entity_id) is not None
    )


def controllable(hass: HomeAssistant, entry: ConfigEntry) -> set[str]:
    """The control list as enforced: ticked for control, still shared and still shareable."""
    registry = er.async_get(hass)
    shared = entry.options.get(CONF_ENTITIES, [])[: payload.MAX_ENTITIES]
    excluded = unshared_entries(hass)
    return {
        e for e in entry.options.get(CONF_CONTROL, [])
        if e in shared and (ent := registry.async_get(e)) is not None and shareable(ent, excluded)
    }


def hub_plugin(hub: ConfigEntry) -> str | None:
    """HA-008: the solax_modbus hub entry's plugin option (see payload.plugin_name)."""
    return payload.plugin_name(hub.options.get("plugin") or hub.data.get("plugin"))


def solax_entities(hass: HomeAssistant) -> tuple[dict[str, str], str | None]:
    """BAT-008: the direct VPP entities of the single solax_modbus hub found by role (resolved by unique id), and the inverter device.
    SMB-004: none for a hub of another brand's plugin (no control of Growatt, Solis or Sofar)."""
    hubs = hass.config_entries.async_entries(SOLAX)
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
    pusher = Pusher(hass, entry)
    entry.async_on_unload(async_track_time_interval(hass, pusher.tick, timedelta(seconds=10), name="SunnyHome push"))
    entry.async_on_unload(entry.add_update_listener(_reload))
    runner = CommandRunner(hass, entry)
    # AUTO-004: the leases are kept with or without a control list (an empty list drops them) and whatever the token's state,
    # so a lease still ends its device after an unlink.
    await runner.start_leases()
    if entry.options.get(CONF_CONTROL) or entry.options.get(CONF_INVERTER):  # BAT-008: the inverter option alone also claims
        # An entry background task: HA cancels it, and the commands it started, on unload or reload.
        entry.async_create_background_task(hass, runner.run(), "SunnyHome commands")
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Saving the options (or a new token at reauth) restarts the pusher."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ir.async_delete_issue(hass, DOMAIN, ISSUE)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """AUTO-004: removing the integration forgets its leases; the devices stay as they are. BAT-008: an inverter lease gets a
    best-effort exit first."""
    await storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.leases").async_remove()
    store = storage.Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.inverter")
    if await store.async_load() and (remote := solax_remote(hass)):
        try:
            await hass.services.async_call(
                "select", "select_option", {"entity_id": remote.mode, "option": payload.SELECT_DISABLED}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 - best effort; the inverter's own timeout is the backstop
            _LOGGER.debug("Inverter exit at removal failed: %s", type(err).__name__)
    await store.async_remove()


class Pusher:
    """Collects the profile readings and the shared devices and posts them, with backoff and reauth."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass, self.entry = hass, entry
        self.url = f"{entry.data[CONF_URL]}/api/ha/v1/states"
        self.headers = {"Authorization": f"Bearer {entry.data[CONF_TOKEN]}"}
        self.busy = self.stopped = self.warned = False
        self.delay = 0
        self.retry_at = 0.0

    async def tick(self, _now: datetime) -> None:
        if self.busy or self.stopped or time.monotonic() < self.retry_at:
            return
        body = self.collect()
        self.busy = True
        try:
            await self.post(body)
        finally:
            self.busy = False

    def collect(self) -> dict:
        """Body with the shared devices; "solax" is null unless exactly one inverter integration entry exists (GW-002: a goodwe
        entry posts "inverter" in its place; DEYE-003: so does a Deye entry with a valid serial, else "solax" stays null)."""
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
        return payload.build(
            dt_util.utcnow(), serial, readings, self.entities(), controllable(self.hass, self.entry), self.sensors(),
            self.remote(readings) if solax else None, self.settings(hub, profile) if profile else None, domain, info,
        )

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
        device = dr.async_get(self.hass).async_get(device_id) if device_id else None
        try:
            plugin = loader.async_get_loaded_integration(self.hass, SOLAX).version
        except Exception:  # noqa: BLE001 - not loaded: no plugin version, so never verified
            plugin = None
        return payload.remote_report(
            True, state.state if state else None, max(times, default=None), plugin,
            device.model if device else None, device.sw_version if device else None,
        )

    def entities(self) -> list[payload.Entity]:
        """The ticked devices that are still shareable and in the state machine."""
        registry = er.async_get(self.hass)
        items, excluded = [], unshared_entries(self.hass)
        for entity_id in self.entry.options.get(CONF_ENTITIES, [])[: payload.MAX_ENTITIES]:
            ent = registry.async_get(entity_id)
            if ent is None or not shareable(ent, excluded) or (state := self.hass.states.get(entity_id)) is None:
                continue
            modes = state.attributes.get("supported_color_modes")
            items.append(payload.Entity(
                entity_id, state.name, state.state, state.attributes.get("brightness"),
                ent.domain == "light" and brightness_supported(modes), state.last_changed,
            ))
        return items

    def sensors(self) -> list[payload.Sensor] | None:
        """The ticked sensors still shareable and in the state machine; None (no "sensors" key) while none is ticked (HA-005)."""
        ticked = self.entry.options.get(CONF_SENSORS, [])
        if not ticked:
            return None
        registry = er.async_get(self.hass)
        items, excluded = [], unshared_entries(self.hass)
        for entity_id in ticked[: payload.MAX_SENSORS]:
            ent = registry.async_get(entity_id)
            if ent is None or not sensor_shareable(ent, excluded) or (state := self.hass.states.get(entity_id)) is None:
                continue
            items.append(payload.Sensor(
                entity_id, state.name, ent.device_class or ent.original_device_class, state.state,
                state.attributes.get("unit_of_measurement"), state.last_reported,
            ))
        return items

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
            if serial is None and not brand and key in SERIAL_KEYS and ent.device_id and (device := devices.async_get(ent.device_id)):
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

    async def post(self, body: dict) -> None:
        """Never logs the token, payload values or device names; only counts."""
        status = None
        try:
            async with async_get_clientsession(self.hass).post(
                self.url, json=body, headers=self.headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                status = resp.status
                if status == 200:
                    inverter = body.get("solax") or body.get("inverter") or {}
                    _LOGGER.debug(
                        "Posted %d readings, %d settings, %d devices and %d sensors: %s",
                        len(inverter.get("readings", [])), len((inverter.get("settings") or {}).get("states", {})),
                        len(body["entities"]), len(body.get("sensors", [])),
                        (await resp.text())[:200],
                    )
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Post failed: %s", type(err).__name__)
        if status == 200:
            self.delay, self.retry_at = 0, 0.0
        elif status == 401:
            self.stopped = True
            _LOGGER.warning("SunnyHome refused the token; pair again with a new code")
            self.entry.async_start_reauth(self.hass)
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
        self.env = payload.Env(
            shared["locks"], shared.setdefault("inflight", {}),
            lambda entity_id: entity_id in controllable(hass, entry), self.seen, self.dispatch,
            leases=self.leases, save=self.save,
        )

    async def start_leases(self) -> None:
        """Loads the stored leases (a lease that ended while HA was down is ended at the next check, reported as a restart)."""
        self.leases.items.update(payload.Leases(await self.store.async_load()).items)
        self.restarted = {eid for eid, _ in self.leases.due(time.time())}
        self.entry.async_on_unload(async_track_time_interval(self.hass, self.lease_tick, timedelta(seconds=LEASE_TICK_S), name="SunnyHome leases"))
        # HA's callback marks a function object; a bound method refuses the attribute (AttributeError), so wrap it.
        self.entry.async_on_unload(async_track_state_change_event(self.hass, sorted(controllable(self.hass, self.entry)),
                                                                  callback(lambda event: self.state_changed(event))))
        self.entry.async_create_background_task(self.hass, self.send_reports(), "SunnyHome lease reports")
        # BAT-008: the inverter lease survives unload, reload and restart; the tick also ends it when the option was turned off.
        self.inverter.load(await self.inverter_store.async_load())
        self.entry.async_on_unload(lambda: setattr(self.inverter, "unloaded", True))
        self.entry.async_on_unload(async_track_time_interval(
            self.hass, self.inverter.tick, timedelta(seconds=LEASE_TICK_S), name="SunnyHome inverter lease"
        ))

    async def save_inverter(self, data: dict | None) -> None:
        if data is None:
            await self.inverter_store.async_remove()
        else:
            await self.inverter_store.async_save(data)

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
                if now > lease.until + LEASE_GIVE_UP_S:  # ponytail: a device not seen off for a day is given up
                    await self._drop(eid, lease, "lease_cancelled", None)
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

    async def _drop(self, eid: str, lease: payload.Lease, outcome: str, reason: str | None) -> None:
        self.leases.cancel(eid)
        await self.save()
        self.off_sent.pop(eid, None)
        self.restarted.discard(eid)
        self.report(lease.command, outcome, reason)

    def state_changed(self, event) -> None:
        """A change that leaves a lease's target and is not this component's own call ends the lease (reported as outside_change)."""
        data = event.data
        eid, old, new = data.get("entity_id"), data.get("old_state"), data.get("new_state")
        lease = self.leases.items.get(eid)
        if lease is None or old is None or new is None or new.state in ("unavailable", "unknown"):
            return  # no lease, HA starting or the entity gone: not a change by anyone
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
                    cid, cmd = (payload.parse_inverter if inverter else payload.parse_command)(item)
                    if cid is not None:  # an invalid command without a valid id is dropped
                        self.entry.async_create_background_task(
                            self.hass, (self.execute_inverter if inverter else self.execute)(cid, cmd, sent), "SunnyHome command"
                        )
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

    async def result(self, body: dict, until: float) -> None:
        delay = 1
        # The server takes the result once and answers 200 to repeats; retry until 30 s past the deadline.
        while (await self.post("result", body, 10))[0] not in (200, 401, 404, 422) and time.monotonic() < until + 30:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)

    def seen(self, entity_id: str) -> payload.Seen | None:
        if (state := self.hass.states.get(entity_id)) is None:
            return None
        attrs = state.attributes
        return payload.Seen(
            state.state, attrs.get("brightness"),
            state.domain == "light" and brightness_supported(attrs.get("supported_color_modes")),
            attrs.get("assumed_state") is True,
        )

    def dispatch(self, domain: str, service: str, data: dict) -> asyncio.Task:
        # AUTO-004: its own context, so a lease does not take this call's state change for one from another context.
        context = Context()
        self.contexts[data["entity_id"]] = context.id
        # A hass task, never cancelled (not even on unload): the entity stays busy until the call ends.
        task = self.hass.async_create_task(
            self.hass.services.async_call(domain, service, data, blocking=True, context=context), "SunnyHome service call", eager_start=True
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
        if path == "claim" and self.entry.options.get(CONF_INVERTER):  # BAT-008: inverter commands only while the option is on
            headers["X-SunnyHome-Inverter"] = "1"
            headers["X-SunnyHome-Component"] = VERSION  # RA-004: from 0.6.0 SunnyHome also sends no_discharge (mode 7)
        try:
            async with async_get_clientsession(self.hass).post(
                f"{self.url}/{path}", json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                return resp.status, await resp.json(content_type=None) if resp.status == 200 else None
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Command %s failed: %s", path, type(err).__name__)
            return None, None

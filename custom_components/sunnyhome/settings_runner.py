"""DEYE-013 (TASK-427, docs/28 sections 5.3 and 6): Home Assistant around the pure settings controller (settingsctl.py).

It finds the inverter entry its SunnyHome entry reads (HA-027: link_inverter) when that integration has a profile with a settingsControl binding
(the profile is the one whose "row" is that integration), resolves the profile's entities by the profile's unique id pattern, runs
the queue ops the backend sends (apply with catalog targets, exit, restore, release, rebase, clear_restore), the lease tick and the
lifecycle restores, and keeps the mirror in one fixed-key store, sunnyhome.setting_snapshots (serial -> device). Nothing here names a brand.
"""

from __future__ import annotations

import copy
import math
import logging
import re
import time
from typing import Callable

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import storage

from . import settingsctl
from .const import CONF_BACKUP_LOADS, CONF_SETTINGS_CONTROL, DOMAIN

_LOGGER = logging.getLogger(__name__)
STORE_KEY = f"{DOMAIN}.setting_snapshots"
ISSUE = "settings_restore_pending"
ISSUE_VALUES = "settings_not_restored"  # DEYE-030: the owner's values to set by hand (removal, an inverter entry gone)
ISSUE_LEFT = "settings_program_left"  # DEYE-030 (C11): the owner took the program back with SunnyHome's slots in it
# DEYE-030 re-review N3: the one table of settings ops: whether a claimed item with this op is the settings controller's (present: a
# settings-controlled inverter is the entry). SettingsRunner handles each op in its method _op_<op>.
OPS: dict[str, Callable[[dict, bool], bool]] = {
    "apply": lambda item, present: "targets" in item or "program" in item,  # a lease apply or a window; else the timed-mode inverter's
    "schedule": lambda item, present: True,
    "exit": lambda item, present: present,
    "restore": lambda item, present: True,
    "release": lambda item, present: True,
    "rebase": lambda item, present: True,
    "clear_restore": lambda item, present: True,
}
BINDINGS = settingsctl.bindings()


def binding_of(integration: str | None) -> settingsctl.Binding | None:
    return BINDINGS.get(integration) if integration else None


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMING_KEYS = ("restoreTryMinutes", "graceS", "readS", "startupGraceS")


def valid_timings(value: object) -> bool:
    """The states answer's "settingsControl": the core's timings (restore try offsets from 0, ascending; the grace, read and startup grace in s)."""
    if not isinstance(value, dict) or any(k not in value for k in TIMING_KEYS):
        return False
    tries = value["restoreTryMinutes"]
    return (isinstance(tries, list) and tries and tries[0] == 0 and all(type(t) is int for t in tries) and tries == sorted(set(tries))
            and all(type(value[k]) is int and value[k] > 0 for k in TIMING_KEYS[1:]))


def settings_op(item: object, present: bool) -> str | None:
    """The command id when the claimed item is a settings op (one carrying targets, an instance-bound op, or an exit while a settings-controlled
    inverter is the entry); None for anything else (the timed-mode inverter path takes it)."""
    if not isinstance(item, dict) or not isinstance(cid := item.get("id"), str) or not _UUID.fullmatch(cid) or type(item.get("ttlMs")) is not int:
        return None
    op = item.get("inverter")
    taken = OPS.get(op) if isinstance(op, str) else None
    return cid if taken is not None and taken(item, present) else None


def no_backup(options) -> bool:
    """D3: only an explicit "no backup loads" answer lets writes go; no answer refuses."""
    return options.get(CONF_BACKUP_LOADS) == "no"


def binding_by_id(profile: object) -> settingsctl.Binding | None:
    return next((b for b in BINDINGS.values() if b.id == profile), None)


def owner_values(roles: dict[str, object], binding: settingsctl.Binding | None, entity: Callable[[str], str | None] | None = None) -> str:
    """DEYE-030: the owner's values for a notice, each by its entity (the inverter's own entity id, else the profile's), never a role key."""
    def name(key: str) -> str:
        return (entity and entity(key)) or (binding.settings.get(key, {}).get("entity") if binding else None) or key
    return ", ".join(f"{name(k)} = {v}" for k, v in roles.items())


class SettingsRunner:
    """One per SunnyHome entry; acts on the settings-controlled inverter entry this SunnyHome entry reads (inverter() returns its domain, entry and serial).
    DEYE-030 (C5): every runner shares one copy of the store (hass.data) and acts only on a mirror of its own entry, or one whose entry is gone
    or not running (DEYE-036 C2: running(owner)). DEYE-036 (C1, C8): each mirror records its inverter entry; hub(id) gives (domain, inverter
    entry, device id) of that entry while it is there with a binding, so a mirror of an inverter this entry no longer reads is still let go."""

    def __init__(self, hass, entry, inverter: Callable[[], tuple | None], timings: Callable[[], dict | None],
                 hub: Callable[[str], tuple | None], running: Callable[[str], bool]) -> None:
        self.hass, self.entry, self.inverter, self.timings, self.hub, self.running = hass, entry, inverter, timings, hub, running
        self.store = storage.Store(hass, 1, STORE_KEY)
        self.data: dict = {}
        self.ctl: settingsctl.Controller | None = None
        self.left: dict[str, settingsctl.Controller] = {}  # C1: controllers of this entry's mirrors on an inverter it no longer reads
        self._op: settingsctl.Controller | None = None  # C4: the controller of the op that runs; a report never swaps it
        self.started = time.time()
        self.zone: str | None = None

    async def load(self) -> None:
        shared = self.hass.data.setdefault(DOMAIN, {})
        if not isinstance(shared.get(STORE_KEY), dict):
            loaded = await self.store.async_load()
            shared.setdefault(STORE_KEY, loaded if isinstance(loaded, dict) else {})  # a runner that loaded meanwhile keeps its copy
        self.data = shared[STORE_KEY]

    async def save(self, ctl: settingsctl.Controller | None = None) -> None:
        """Stores the given controller's mirror (the live one by default) under its own serial, with this entry and its inverter entry (C4:
        never another controller's)."""
        ctl = ctl or self.ctl
        if ctl is not None and ctl.d is not None:
            self.data[ctl.serial] = {**copy.deepcopy(ctl.d), "entry": self.entry.entry_id, "hub": ctl.hub}
        elif ctl is not None and ctl.serial in self.data:
            self.data.pop(ctl.serial)
        if ctl is not None and ctl is self.ctl:
            self._seen = self.data.get(self._serial)
        await self.store.async_save(self.data)

    _serial: str | None = None
    _seen: object = None  # the stored copy this runner's controller was loaded from or saved last (DEYE-030 re-review N1)

    def mine(self, stored: object) -> bool:
        """A stored mirror of this entry, an idle one, or one of an entry that is gone or not running (a re-added entry adopts it; C2: a disabled
        or failed one's inverter is read by another entry now); never another running entry's held or pending mirror."""
        if not isinstance(stored, dict):
            return False
        owner = stored.get("entry")
        return owner in (None, self.entry.entry_id) or stored.get("state") == "idle" or not self.running(owner)

    def holds(self) -> bool:
        """Whether a stored mirror of this entry still owes a restore (its ops must reach the inverter with the option off)."""
        return any(m.get("state") != "idle" for m in self.data.values() if self.mine(m))

    def _controller(self) -> settingsctl.Controller | None:
        """The controller of the current inverter entry, its mirror loaded by serial (bound to this SunnyHome entry); None without one."""
        if self._op is not None:
            return self._op  # C4: a serial reported while an op runs never swaps the op's controller (the next call takes it)
        found = self.inverter()
        if found is None:
            return None
        domain, inv_entry, serial, device_id = found
        b = binding_of(domain)
        if b is None or serial is None:
            return None
        stored = self.data.get(serial)
        if stored is not None and not self.mine(stored):
            return None  # DEYE-030 (C5): another SunnyHome entry holds this inverter
        if self.ctl is not None and self.ctl.b is b and self._serial == serial and stored is not self._seen:
            self.ctl.load(copy.deepcopy(stored) if isinstance(stored, dict) else None, serial)  # re-review N1: another entry saved it since
            self._seen = stored
        if self.ctl is not None and self.ctl.d is not None and self.ctl.d["state"] == "armed" and self._serial not in (None, serial):
            return self.ctl  # re-review N7: a new serial under a held mirror: that mirror is let go (the tick) before it is left
        if self.ctl is None or self.ctl.b is not b or self._serial != serial:
            self.left.pop(serial, None)  # C1: the inverter is read again: its live controller takes the mirror over
            self.ctl = self._build(domain, inv_entry, serial, device_id, stored)  # a re-added entry adopts a held or pending mirror: never a second snapshot
            self._serial, self._seen = serial, stored
        return self.ctl

    def _build(self, domain: str, inv_entry, serial: str, device_id: str | None, stored: object) -> settingsctl.Controller:
        """A controller of one inverter entry, its mirror loaded from stored (the live one, or C1 one of a mirror left on another inverter)."""
        b = binding_of(domain)
        registry = er.async_get(self.hass)

        def entity(key: str) -> str | None:
            if key not in b.settings:
                return None
            domain_name = b.settings[key]["entity"].split(".", 1)[0]
            return registry.async_get_entity_id(domain_name, domain, b.unique_id(inv_entry.entry_id, key))

        def state(entity_id: str) -> str | None:
            s = self.hass.states.get(entity_id)
            return None if s is None else s.state

        def call(domain_name: str, service: str, data: dict):
            """Raises before any frame (the allow-list, a missing service): the write is not_sent; anything later is unknown."""
            if (domain_name, service) not in settingsctl.WRITE_SERVICES and (domain_name, service) != b.readback:
                raise PermissionError(f"{domain_name}.{service}")  # the allow-list: never a raw register write
            if not self.hass.services.has_service(domain_name, service):
                raise LookupError(f"{domain_name}.{service}")
            return self.hass.services.async_call(domain_name, service, data, blocking=True,
                                                 return_response=(domain_name, service) == b.readback)

        io = settingsctl.Io(b, call, state, entity, lambda: device_id)
        ctl = settingsctl.Controller(b, io, time.time)
        ctl.serial, ctl.hub = serial, inv_entry.entry_id  # the store key and the inverter entry the mirror records (C1, C4)
        io.before_write = lambda: self.save(ctl)  # DEYE-030 (C3): the snapshot and the armed state are stored before the first frame
        ctl.clock = lambda: self.clock_reason(b, inv_entry)
        ctl.load(copy.deepcopy(stored) if isinstance(stored, dict) else None, serial)
        return ctl

    def clock_reason(self, b: settingsctl.Binding, inv_entry) -> str | None:
        """TASK-1099: the profile's clock entity against SunnyHome's clock (HA's clock + the states answer's clockOffsetMs, kept apart from
        the armed timings so it is always the latest)."""
        named = (b.control.get("clock") or {}).get("entity")
        if not named:
            return "clock_unknown"
        key_domain, key = named.split(".", 1)
        entity = er.async_get(self.hass).async_get_entity_id(key_domain, inv_entry.domain, b.control["uniqueId"].format(
            entry=inv_entry.entry_id.lower(), key=key, platform=key_domain))
        s = self.hass.states.get(entity) if entity else None
        offset = (self.timings() or {}).get("clockOffsetMs")
        zone = (self.ctl.d or {}).get("zone") if self.ctl else self.zone
        return settingsctl.clock_check(None if s is None else s.state, None if s is None else s.last_updated.timestamp(),
                                       None if not isinstance(offset, (int, float)) else offset / 1000, self.hass.config.time_zone, zone,
                                       b.capability("clockToleranceSeconds"), b.stale_s, time.time())

    def entity_gone(self, ctl: settingsctl.Controller) -> bool:
        """A held role whose entity left the registry, once the startup grace has passed."""
        timings = (ctl.d or {}).get("timings")
        return bool(timings) and time.time() >= self.started + timings["startupGraceS"] and any(ctl.io.entity(k) is None for k in ctl.held())

    def backup_loads(self, ctl: settingsctl.Controller) -> bool:
        """D3: the options answer, or the profile's backup-load reading above 0; a reading that is missing or unavailable counts as loads."""
        return self._backup(ctl) is not False

    def _backup_lets_go(self, ctl: settingsctl.Controller) -> bool:
        """Re-review N6 and delta #6: loads let a held mirror go at once; an unknown reading only once it has lasted the held read, after the
        startup grace (an entity that is briefly unavailable, a restart)."""
        state, now = self._backup(ctl), time.time()
        if state is not None:
            self._unknown_since = None
            return state
        self._unknown_since = self._unknown_since or now
        t = (ctl.d or {}).get("timings") or {}
        return now - self._unknown_since >= t.get("readS", 0) and now >= self.started + t.get("startupGraceS", 0)

    _unknown_since: float | None = None

    def _backup(self, ctl: settingsctl.Controller) -> bool | None:
        """True: loads (the answer, or a reading above 0); False: none; None: the reading is missing or unreadable."""
        if not no_backup(self.entry.options):
            return True
        named = ctl.b.control.get("backupLoad", {}).get(ctl.io.definition)
        if not named:
            return False
        key_domain, key = named.split(".", 1)
        found = self.inverter()
        entity = er.async_get(self.hass).async_get_entity_id(key_domain, found[0], ctl.b.control["uniqueId"].format(
            entry=found[1].entry_id.lower(), key=key, platform=key_domain)) if found else None
        s = self.hass.states.get(entity) if entity else None
        try:
            value = float(s.state) if s is not None else math.nan
        except ValueError:
            value = math.nan  # DEYE-030 (Low): unavailable or unknown is not "no load"
        return None if math.isnan(value) else value > 0  # re-review N6: nan is no "no load" either

    # ---- ops ----
    async def execute(self, item: dict) -> tuple[str, str | None]:
        """One claimed op; (outcome, reason): failed = nothing written. DEYE-030: the mirror is saved however the op ends (a cancellation
        included), and an op that refuses after a frame left is reported unknown, never failed (C6)."""
        op = item.get("inverter")
        ctl = self._controller()
        if ctl is None:  # re-review N2: an op with a _stored_<op> handler (the OWNER's clear) needs no inverter entry
            stored = getattr(self, f"_stored_{op}", None) if isinstance(op, str) and op in OPS else None
            return await stored(item) if stored else ("failed", "entities_missing")
        if not isinstance(op, str) or op not in OPS:
            return "failed", "invalid_command"
        if isinstance(item.get("zone"), str):
            self.zone = item["zone"]
        frames = ctl.io.frames
        self._op = ctl
        try:
            outcome, reason = await getattr(self, f"_op_{op}")(ctl, item)
        finally:
            self._op = None
            if ctl.d is not None:
                self._issues(ctl)  # re-review N5: an op that ends restore_pending clears its notices
            await self.save(ctl)
        if outcome == "failed" and ctl.io.frames != frames:
            return "unknown", reason
        return outcome, reason

    async def _op_release(self, ctl: settingsctl.Controller, item: dict) -> tuple[str, str | None]:
        return ("done", None) if ctl.release(item.get("instance")) else ("failed", "instance_mismatch")

    async def _op_rebase(self, ctl: settingsctl.Controller, item: dict) -> tuple[str, str | None]:
        return ("done", None) if ctl.rebase(item.get("instance"), item.get("baselines") or {}) else ("failed", "instance_mismatch")

    async def _op_clear_restore(self, ctl: settingsctl.Controller, item: dict) -> tuple[str, str | None]:
        if ctl.clear(item.get("instance")):
            return "done", None
        outcome, _ = await self._stored_clear_restore(item)  # delta #5: a pending mirror under another serial
        return (outcome, None) if outcome == "done" else ("failed", "restore_not_pending")

    async def _stored_clear_restore(self, item: dict) -> tuple[str, str | None]:
        """The OWNER's clear of a pending mirror of this entry that no controller reaches (its inverter entry gone)."""
        instance = item.get("instance")
        for serial, m in self.data.items():
            b = binding_by_id(m.get("profile")) if isinstance(m, dict) else None
            if b is not None and self.mine(m) and m.get("state") == "restore_pending" and m.get("instance") == instance:
                ctl = settingsctl.Controller(b, None, time.time)
                ctl.load(copy.deepcopy(m), serial)
                ctl.clear(instance)
                self.data[serial] = {**ctl.d, "entry": self.entry.entry_id, "hub": m.get("hub")}
                self.left.pop(serial, None)
                await self.store.async_save(self.data)
                ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE}_{serial}")
                ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_VALUES}_{serial}")
                return "done", None
        return "failed", "entities_missing"

    async def _detected(self, ctl: settingsctl.Controller) -> str | None:
        return await ctl.io.detect() if ctl.io.definition is None else None

    async def _op_exit(self, ctl: settingsctl.Controller, item: dict) -> tuple[str, str | None]:
        """An intent's Stop: the lease and an open window end; a day's program stays (DEYE-023 approach table)."""
        if (refused := await self._detected(ctl)) is not None:
            return "failed", refused
        reason = None
        if ctl.d is not None:
            reason, _ = await ctl.restore("lease")
            if ctl.d.get("program", {}).get("window"):
                reason, _ = await ctl.end_window()
        await self._orphans(self.inverter())  # C1: this entry's mirror on an inverter it no longer reads is let go too
        if ctl.d is not None and (ctl.held("lease") or ctl.d.get("program", {}).get("window")):
            return "unknown", reason
        if self._armed_elsewhere(ctl):
            return "unknown", None  # C1: never done while one of this entry's mirrors still holds SunnyHome's values (the live device is not pending)
        return "done", None

    async def let_go_left(self) -> bool:
        """C1: for another controller's exit (the timed-mode inverter now picked): this entry's mirrors left on another inverter are let go;
        True while one of them still holds SunnyHome's values (that exit is then never done). A tick: the live controller lets go too.
        """
        await self.tick()
        return any(m.get("state") == "armed" for m in self.data.values() if self.mine(m))

    def _armed_elsewhere(self, ctl: settingsctl.Controller, owed: bool = False) -> bool:
        """C1: a mirror of this entry under another serial (another inverter, a changed pick) still holds SunnyHome's values; with owed (an
        apply) also one of the same inverter entry that still owes its restore (a new snapshot would take SunnyHome's values as the owner's)."""
        return any(serial != ctl.serial and (m.get("state") == "armed" or owed and m.get("state") != "idle" and m.get("hub") == ctl.hub)
                   for serial, m in self.data.items() if self.mine(m))

    async def _op_restore(self, ctl: settingsctl.Controller, item: dict) -> tuple[str, str | None]:
        if ctl.d is None or ctl.d.get("instance") != item.get("instance"):
            return "failed", "instance_mismatch"
        if (refused := await self._detected(ctl)) is not None:
            return "failed", refused
        reason, _ = await ctl.restore()
        return ("done", None) if ctl.d["state"] == "idle" else ("unknown", reason)

    async def _op_schedule(self, ctl: settingsctl.Controller, item: dict) -> tuple[str, str | None]:
        return await self._op_apply(ctl, item, schedule=True)

    async def _op_apply(self, ctl: settingsctl.Controller, item: dict, schedule: bool = False) -> tuple[str, str | None]:
        """A lease apply, a window, or a day's program (schedule): refused before any write without the option, the timings, the backup
        answer or the definition."""
        if (refused := await self._detected(ctl)) is not None:
            return "failed", refused
        if ctl.d is not None and (found := self.inverter()) is not None and found[2] not in (None, ctl.d["serial"]) or self._armed_elsewhere(ctl, owed=True):
            return "failed", "busy"  # re-review N7, C1: the previous inverter's mirror is still being let go
        timings = self.timings()
        if not self.entry.options.get(CONF_SETTINGS_CONTROL):
            return "failed", "inverter_control_off"
        if timings is None:
            return "failed", "controller_off"  # arms nothing without the server's timings
        if self.backup_loads(ctl):
            return "failed", "backup_loads"
        targets, duration, program = item.get("targets"), item.get("durationS"), item.get("program")
        ctl.device(ctl.serial, timings, self.zone)
        if schedule and isinstance(program, dict) and isinstance(program.get("day"), str) and isinstance(program.get("slots"), list):
            reason, _ = await ctl.schedule(program["day"], program["slots"])  # unit 6 sends it (TASK-432); tested with fake ops here
        elif not schedule and isinstance(program, dict) and type(duration) is int:
            reason, _ = await ctl.window(program, time.time() + duration, item.get("active") or item.get("kind"))
        elif not schedule and isinstance(targets, dict) and type(duration) is int:
            reason, _ = await ctl.apply(item.get("active") or item.get("kind"), {k: str(v) for k, v in targets.items()}, time.time() + duration)
        else:
            return "failed", "invalid_command"
        if reason is None:
            return "done", None
        return ("unknown", reason) if reason == "unknown" else ("failed", reason)

    # ---- the tick (every LEASE_TICK_S) ----
    async def tick(self) -> None:
        found = self.inverter()
        # DEYE-030 post review #1: an unknown serial (no reading yet, a lost attribute) keeps the live controller; only a gone inverter entry, an
        # integration without a binding, another posted serial or (C1) another inverter entry orphans a mirror
        ctl = self._controller() if found and found[2] is not None else self.ctl
        await self._orphans(found)
        if ctl is not None and self._show_left(ctl):
            await self.save(ctl)
        if ctl is None or ctl.d is None or ctl.d["state"] == "idle" and not (ctl.d.get("program") or {}).get("pending"):
            self._unknown_since = None  # delta review nit: an unknown backup reading is timed per held mirror
            return
        before = copy.deepcopy(ctl.d)  # a counted try changes only the roles: it is saved too
        try:
            await self._tick(ctl)
        finally:
            if ctl.d != before:
                await self.save(ctl)  # C4: a report may have swapped the live controller meanwhile

    async def _tick(self, ctl: settingsctl.Controller, left: bool = False) -> None:
        timings, now = ctl.d.get("timings") or {}, time.time()
        paced = now >= max(ctl.last_read, ctl.last_restore) + timings.get("readS", 0)  # an unreachable inverter is asked at the held read's cadence, not every tick
        # letting go: the option off, the backup-loads answer no longer "no" (DEYE-030 Low), the inverter entry gone, a role entity gone
        # re-review N6: the backup-load reading above 0 (or unknown) too; N7: another serial posted under a held mirror lets it go first
        # C1: a mirror on an inverter this entry no longer reads (left) is always let go
        found = self.inverter()
        letting_go = left or not self.entry.options.get(CONF_SETTINGS_CONTROL) or found is None or self.entity_gone(ctl) or self._backup_lets_go(ctl) \
            or found[2] is not None and found[2] != ctl.d["serial"]
        if letting_go and (ctl.d.get("program") or {}).get("pending"):
            ctl.d["program"]["pending"] = None  # delta post review N1: a program that waits is never written after letting go
            if ctl.d["state"] == "idle":
                return
        if ctl.io.definition is None:
            if not paced:
                return
            ctl.last_read = now
            await ctl.io.detect()  # a restart: nothing can be read before the definition is known
        if ctl.d["state"] == "armed" and letting_go:
            if paced or ctl.last_restore == 0:
                await ctl.restore()
        else:
            due = ctl.due()
            detected = ctl.io.definition
            if due == "check" and detected is not None and (await ctl.io.detect()) is None and ctl.io.definition != detected:
                await ctl.restore()  # the definition changed under a held role (checked at the held read's cadence)
            elif due == "restore":
                await ctl.restore("lease")  # post review #1: a lease end never touches the program
            elif due == "restore_program":
                await ctl.restore("program")
            elif due == "window_lapse":
                await ctl.end_window()  # TASK-1099: a lapsed window: the day's program, or the owner's
            elif due == "window_end":
                await ctl.window_ended()
            elif due == "midnight":
                await ctl.midnight()
            elif due == "repeat_guard":
                await ctl.repeat_guard()
            elif due == "check":
                await ctl.check()
        if self.entity_gone(ctl) and ctl.held():
            ctl.d.update(state="restore_pending", reason="restore_refused", pendingSince=ctl.d.get("pendingSince") or time.time())
        self._issues(ctl)

    def _issues(self, ctl: settingsctl.Controller) -> None:
        """The repair issues of the device after a tick or an op."""
        if ctl.d["state"] == "restore_pending":
            ir.async_create_issue(self.hass, DOMAIN, f"{ISSUE}_{ctl.d['serial']}", is_fixable=False, severity=ir.IssueSeverity.ERROR, translation_key=ISSUE,
                                  translation_placeholders={"inverter": self.label(ctl.serial, ctl.hub)})
        else:  # DEYE-030 (Low): a pending restore that a read or an op resolved; the values notice of this inverter too
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE}_{ctl.d['serial']}")
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_VALUES}_{ctl.d['serial']}")
        if ctl.held("program"):
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_LEFT}_{ctl.d['serial']}")  # a new program of SunnyHome's: the earlier notice is over
        self._show_left(ctl)

    def _show_left(self, ctl: settingsctl.Controller) -> bool:
        """DEYE-030 (C11): the owner took the program back with SunnyHome's values in it: their own earlier values, shown once."""
        left = (ctl.d.get("program") or {}).pop("left", None) if ctl.d else None
        if left:
            ir.async_create_issue(self.hass, DOMAIN, f"{ISSUE_LEFT}_{ctl.d['serial']}", is_fixable=False, is_persistent=True, severity=ir.IssueSeverity.WARNING,
                                  translation_key=ISSUE_LEFT, translation_placeholders={"values": owner_values(left, ctl.b, ctl.io.entity),
                                                                                        "inverter": self.label(ctl.serial, ctl.hub)})
        return bool(left)

    async def _orphans(self, found: tuple | None) -> None:
        """A mirror of this entry that the live controller does not hold (another inverter picked, another serial posted): C1, C8 let it go
        through the inverter entry it recorded while that entry is there; DEYE-030 (C10) otherwise (the inverter entry gone, its integration
        without a binding) nothing can restore it, so after the startup grace it is restore_pending and the owner is shown the values."""
        bound = found is not None and binding_of(found[0]) is not None
        current = found[2] if bound else None
        now, changed = time.time(), False
        for serial, m in list(self.data.items()):
            if serial == current or self.ctl is not None and serial == self._serial or not self.mine(m) or m.get("state") == "idle":
                if not self.mine(m):
                    self.left.pop(serial, None)  # another running entry holds it now: rebuilt from its store copy if it comes back
                continue  # the live controller's own mirror: its letting go and entity_gone own it (delta review N1)
            if bound and current is None and m.get("hub") in (None, found[1].entry_id):
                continue  # post review #1: the serial of this inverter entry is not known yet: the mirror may be its own
            if (ctl := self._left(serial, m)) is not None and ctl.d is not None:
                before = copy.deepcopy(ctl.d)
                await self._tick(ctl, left=True)
                if ctl.d != before:
                    await self.save(ctl)
                if ctl.d["state"] == "idle":
                    self.left.pop(serial, None)
            elif m.get("state") == "armed" and now >= self.started + (m.get("timings") or {}).get("startupGraceS", 0):
                m = self.data[serial] = {**m, "state": "restore_pending", "reason": "restore_refused", "pendingSince": m.get("pendingSince") or now}  # C5: a new copy
                not_restored(self.hass, m, serial, self.label(serial, m.get("hub")))
                changed = True
        if changed:
            await self.store.async_save(self.data)

    def _left(self, serial: str, m: dict) -> settingsctl.Controller | None:
        """C1, C8: the controller of this entry's mirror on an inverter entry it does not read now, by the inverter entry it recorded; None once
        that entry is gone or its integration has no binding for the mirror's profile."""
        if (ctl := self.left.get(serial)) is not None:
            return ctl
        found = self.hub(m["hub"]) if isinstance(m.get("hub"), str) else None
        if found is None or getattr(binding_of(found[0]), "id", None) != m.get("profile"):
            return None
        self.left[serial] = self._build(found[0], found[1], serial, found[2], m)
        return self.left[serial]

    def label(self, serial: str, hub: str | None) -> str:
        """C6: the inverter a notice is about: its inverter entry's title and serial, or the serial alone once that entry is gone."""
        found = self.hub(hub) if isinstance(hub, str) else None
        return f"{found[1].title} ({serial})" if found is not None else serial

    def report(self) -> dict | None:
        """The settings.control block; an idle one as soon as the device is known, so SunnyHome can take the first apply."""
        ctl = self._controller()
        return None if ctl is None else ctl.known(self._serial, self.timings(), self.zone)


def not_restored(hass, mirror: dict, serial: str, inverter: str, entity: Callable[[str], str | None] | None = None) -> None:
    """DEYE-030: the translated repair issue (one per inverter, C6: named) with the owner's values of the held roles, for them to set by hand."""
    values = {k: r["snapshot"] for k, r in mirror.get("roles", {}).items() if r.get("held")}
    ir.async_create_issue(hass, DOMAIN, f"{ISSUE_VALUES}_{serial}", is_fixable=False, is_persistent=True, severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_VALUES,
                          translation_placeholders={"values": owner_values(values, binding_by_id(mirror.get("profile")), entity), "inverter": inverter})


async def remove(hass, entry, runner: SettingsRunner) -> None:
    """The SunnyHome entry is removed: one verified restore try of each mirror of this entry (C8: by the inverter entry it recorded, not by the
    inverter the removed entry would read now), then a translated repair issue with the owner's values of whatever is still held, also for a
    mirror no controller can reach (DEYE-030 C10)."""
    live = runner._controller()
    for serial, m in list(runner.data.items()):
        if not runner.mine(m) or m.get("state") == "idle":
            continue
        ctl = live if live is not None and serial == live.serial else runner._left(serial, m)
        if ctl is not None and ctl.d is not None:
            if ctl.io.definition is None:
                await ctl.io.detect()
            await ctl.restore()
            await runner.save(ctl)
        if (m := runner.data.get(serial)) is not None and m.get("state") != "idle":
            not_restored(hass, m, serial, runner.label(serial, m.get("hub")), ctl.io.entity if ctl is not None else None)

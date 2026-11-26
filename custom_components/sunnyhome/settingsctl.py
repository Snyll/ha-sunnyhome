"""DEYE-013 (TASK-427, docs/28-deye-inverter-control.md sections 5.2, 5.3, 5.5 and 6): the component's settings controller, brand-free and
pure (no Home Assistant import; Home Assistant is reached through the callbacks of Io and Controller).

An inverter whose controls are persistent settings is driven through the write binding of its profile (profiles/<profile>.json,
"settingsControl") and its control family (control/<family>.json), both bundled copies of the server's files. Nothing here names a brand,
an entity, a register, an option or a service: every one of them is profile or family data.

Who decides (docs/reviews/DEYE-013.md section 1): the backend core decides what to hold and sends catalog role targets; this module only
executes and mirrors. It fresh-reads, snapshots before the first write, writes the delta, verifies by a fresh read, keeps the baseline and
runs the restores it must run alone (the lease end, the core's restore op, letting go). Its per-role rules are the core's
(SettingsControl.java), checked against the same cases (integration-contracts/settings-restore-cases.json). The timings (restore tries,
the lease grace, the held read, the startup grace) are the server's constants, received in the states answer and stored with the mirror.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, NamedTuple
from zoneinfo import ZoneInfo

_HERE = Path(__file__).parent
_LOGGER = logging.getLogger(__name__)
WRITE_SERVICES = frozenset({("number", "set_value"), ("select", "select_option"), ("switch", "turn_on"), ("switch", "turn_off"), ("time", "set_value")})
CALL_S = 20  # the longest one entity write or read may take before its outcome is unknown
RUN_GAP = 8  # ponytail: registers a grouped read may span between two wanted ones; tune if a logger refuses unmapped addresses


class Read(NamedTuple):
    value: str | None
    valid: bool


class Written(NamedTuple):
    sent: str  # verified | not_sent | unknown
    value: str | None


# ---- the data ----

class Binding:
    """One profile with a settingsControl block, and its family."""

    def __init__(self, profile: dict, family: dict) -> None:
        self.id, self.row = profile["profile"], profile.get("row")
        self.settings, self.control, self.family = profile["settings"], profile["settingsControl"], family
        self.stale_s = int(profile.get("staleSeconds", 0))  # TASK-1099: the oldest a polled reading (the clock entity) may be
        self.register_of = {k: name for name, reg in family["registers"].items() for k in reg["keys"]}
        self.readback = tuple(self.control["readback"].split(".", 1))
        # TASK-1099 (docs/28 section 7): the program scope, from the family (slot roles and the days key); every other role is a lease role
        self.slot_roles: list[dict] = family.get("slotRoles", [])
        self.days_key: str | None = family.get("touDays")
        self.program_keys = frozenset([k for slot in self.slot_roles for k in slot.values()] + ([self.days_key] if self.days_key else []))
        self.day_values: dict[str, list[int]] = family.get("touDayValues", {})
        # DEYE-030 re-review S9 (docs/28 section 12): the battery mode roles, every role a lease form of a mode intent writes, less the roles
        # of the forms of the intents that are a function of their own (the family's functionIntents: the export limit); the core's modeRoles
        self.functions = frozenset(family["functionIntents"])  # required: bindings() refuses a family without it, as the server's loader
        forms = [(kind, f) for kind, fs in family["intents"].items() for f in fs]
        self.mode_roles = frozenset(k for kind, f in forms if kind not in self.functions and f.get("end") == "lease" for k in f.get("roles") or {}) \
            - {k for kind, f in forms if kind in self.functions for k in f.get("roles") or {}}

    def is_mode(self, code: str | None) -> bool:
        """A battery mode intent code (an unknown code counts as a mode); a functionIntents intent is a function of its own."""
        return code not in self.functions

    def lease_roles(self, code: str | None) -> frozenset[str]:
        """The roles the intent's lease form writes (the running mode form of the core's rule)."""
        form = next((f for f in self.family["intents"].get(code or "", []) if f.get("end") == "lease"), None)
        return frozenset((form or {}).get("roles") or {})

    def capability(self, name: str) -> int:
        return self.family["capabilities"][name]

    def definition(self, code: int, option: str | None = None) -> tuple[str, str | None] | None:
        """The definition and variant the definition register's value picks (the variant: the later of detected and the entry option)."""
        table = self.control["definitions"]["table"]
        for row in table:
            if any(int(c, 16) == code for c in row["codes"]):
                variants = [r["variant"] for r in table if r["definition"] == row["definition"] and r["variant"]]
                variant = row["variant"]
                if option in variants and variant in variants and variants.index(option) > variants.index(variant):
                    variant = option
                return row["definition"], variant
        return None

    def runs_on(self, days_value: str | None, iso_day: int) -> bool:
        """Whether a days value runs on the ISO weekday; a value the family does not list (a catch-all) counts as running."""
        return days_value not in self.day_values or iso_day in self.day_values[days_value]

    def only_on(self, iso_day: int) -> str:
        return next(v for v, d in self.day_values.items() if d == [iso_day])

    def no_day(self) -> str:
        return next(v for v, d in self.day_values.items() if not d)

    def reg(self, definition: str, key: str) -> dict | None:
        return self.control["registers"].get(definition, {}).get(key)

    @staticmethod
    def _per(value, variant):
        return value.get(variant) if isinstance(value, dict) else value

    def decode(self, definition: str, variant: str | None, key: str, raw: int) -> str | None:
        """The catalog value of a register value as canonical text, None when it decodes to nothing or is out of range."""
        reg = self.reg(definition, key)
        if reg is None or not isinstance(raw, int):
            return None
        if "raw" in reg:
            code = reg["raw"].get(str(raw))
            return code if code is not None else reg.get("rawBit0") if reg.get("rawBit0") and raw & 1 else None
        if reg.get("encoding") == "hhmm":
            return f"{raw // 100:02d}:{raw % 100:02d}" if raw // 100 < 24 and raw % 100 < 60 else None
        value = raw * (self._per(reg.get("scale", 1), variant) or 1)
        low, high = self._per(reg.get("min"), variant), self._per(reg.get("max"), variant)
        return None if low is not None and value < low or high is not None and value > high else str(value)

    def encodable(self, definition: str | None, variant: str | None, key: str, value: object) -> bool:
        """DEYE-030 (C8): whether a catalog value has a register value (a raw code, a valid hh:mm, or a number on the variant's scale within
        its range), so a value the register cannot hold is refused before any write, never after a day-off step."""
        reg = self.reg(definition, key) if definition else None
        if reg is None or not isinstance(value, str):
            return False
        if "raw" in reg:
            return value in reg["raw"].values()
        if reg.get("encoding") == "hhmm":
            h, _, m = value.partition(":")
            return len(h) == len(m) == 2 and h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60
        try:
            number = float(value)
        except ValueError:
            return False
        if not math.isfinite(number):
            return False
        steps = number / (self._per(reg.get("scale", 1), variant) or 1)
        low, high = self._per(reg.get("min"), variant), self._per(reg.get("max"), variant)
        return steps == int(steps) and not (low is not None and number < low or high is not None and number > high)

    def polled(self, key: str, state: str | None) -> str | None:
        """The catalog value the polled entity shows, as canonical text."""
        if state is None or state in ("unavailable", "unknown"):
            return None
        mapping, domain = self.settings[key], self.settings[key]["entity"].split(".", 1)[0]
        if "values" in mapping:
            return mapping["values"].get(state)
        if domain == "switch":
            return {"on": "true", "off": "false"}.get(state)
        if domain == "time":
            return state[:5]
        try:
            number = float(state)
        except ValueError:
            return None
        return str(int(number)) if number.is_integer() else str(number)

    def call(self, key: str, entity: str, value: str) -> tuple[str, str, dict] | None:
        """The one entity service that writes the catalog value (None: the value has no option)."""
        mapping, domain = self.settings[key], entity.split(".", 1)[0]
        if "values" in mapping:
            option = next((o for o, code in mapping["values"].items() if code == value), None)
            return None if option is None else ("select", "select_option", {"entity_id": entity, "option": option})
        if domain == "switch":
            return ("switch", "turn_on" if value == "true" else "turn_off", {"entity_id": entity})
        if domain == "time":
            return ("time", "set_value", {"entity_id": entity, "time": f"{value}:00"})
        number = float(value)
        return ("number", "set_value", {"entity_id": entity, "value": int(number) if number.is_integer() else number})

    def unique_id(self, entry_id: str, key: str) -> str:
        domain, name = self.settings[key]["entity"].split(".", 1)
        return self.control["uniqueId"].format(entry=entry_id.lower(), key=name, platform=domain)


def bindings() -> dict[str, Binding]:
    """Every bundled profile with a settingsControl block, by its row (the integration the reader posts under)."""
    out = {}
    for path in sorted((_HERE / "profiles").glob("*.json")):
        profile = json.loads(path.read_text("utf-8"))
        if "settingsControl" in profile and profile.get("row"):
            family = json.loads((_HERE / "control" / f"{profile['settingsControl']['family']}.json").read_text("utf-8"))
            functions = family.get("functionIntents")
            if not isinstance(functions, list) or any(f not in family.get("intents", {}) for f in functions):
                # DEYE-030: as the server's loader, a family without functionIntents (or naming an intent it lacks) is refused: nothing is controlled
                _LOGGER.error("Settings family %s refused: functionIntents missing or naming an unknown intent", profile["settingsControl"]["family"])
                continue
            out[profile["row"]] = Binding(profile, family)
    return out


# ---- the device I/O ----

class Io:
    """Fresh reads through the profile's read service and writes through the four entity services only."""

    def __init__(self, binding: Binding, call: Callable[[str, str, dict], Awaitable], state: Callable[[str], str | None],
                 entity: Callable[[str], str | None], device: Callable[[], str | None]) -> None:
        self.b, self._call, self.state, self.entity, self.device = binding, call, state, entity, device
        self.definition: str | None = None
        self.variant: str | None = None
        self.before_write: Callable[[], Awaitable] | None = None  # DEYE-030 (C3): the runner saves the mirror before any frame leaves
        self.frames = 0  # write services that ran: an op that refuses after one of them did write something

    async def _read_run(self, address: int, count: int) -> dict[int, int] | None:
        """One read-service call for count adjacent registers; None: nothing was read."""
        device = self.device()
        if device is None:
            return None
        data = {k: (v.format(device=device, address=address, count=count) if isinstance(v, str) else v) for k, v in self.b.control["readbackData"].items()}
        data = {k: int(v) if k != "device" and isinstance(v, str) and v.isdigit() else v for k, v in data.items()}
        try:
            answer = await asyncio.wait_for(self._call(*self.b.readback, data), CALL_S)
        except Exception:  # noqa: BLE001 - unreachable: nothing was read
            return None
        if not isinstance(answer, dict):
            return None
        out = {}
        for a in range(address, address + count):
            value = answer.get(a, answer.get(str(a)))
            if isinstance(value, int):
                out[a] = value
        return out

    async def _read_raw(self, address: int) -> int | None:
        return ((await self._read_run(address, 1)) or {}).get(address)

    async def _read_many(self, addresses: list[int]) -> dict[int, int]:
        """TASK-1099: adjacent registers in one call (runs bounded by the profile's readbackMaxCount and a small gap); a run that fails falls
        back to one call per register, so one bad address never reads as "nothing read" for the rest."""
        limit = int(self.b.control.get("readbackMaxCount", 1))
        out: dict[int, int] = {}
        runs: list[list[int]] = []
        for a in sorted(set(addresses)):
            if runs and a - runs[-1][0] < limit and a - runs[-1][-1] <= RUN_GAP:
                runs[-1].append(a)
            else:
                runs.append([a])
        for run in runs:
            got = await self._read_run(run[0], run[-1] - run[0] + 1) if len(run) > 1 else None
            if got is None or any(a not in got for a in run):
                got = {}
                for a in run:
                    value = await self._read_raw(a)
                    if value is not None:
                        got[a] = value
            out.update({a: got[a] for a in run if a in got})
        return out

    async def detect(self, option: str | None = None) -> str | None:
        """Reads the definition register; None, or definition_unsupported / unreadable."""
        raw = await self._read_raw(int(self.b.control["definitions"]["register"], 16))
        if raw is None:
            return "unreadable"
        found = self.b.definition(raw, option)
        if found is None:
            return "definition_unsupported"
        self.definition, self.variant = found
        return None

    async def read(self, keys) -> dict[str, Read]:
        wanted = {}
        for key in keys:
            reg = self.b.reg(self.definition, key) if self.definition else None
            entity = self.entity(key)
            if reg is not None and entity is not None:
                wanted[key] = (int(reg["address"], 16), entity)
        raws = await self._read_many([a for a, _ in wanted.values()]) if wanted else {}
        out = {}
        for key, (address, entity) in wanted.items():
            if address not in raws:
                continue  # unreachable: nothing read
            value = self.b.decode(self.definition, self.variant, key, raws[address])
            out[key] = Read(value, value is not None and value == self.b.polled(key, self.state(entity)))
        return out

    async def write(self, values: dict[str, str]) -> dict[str, Written]:
        out = {}
        if values and self.before_write is not None:
            await self.before_write()
        for key, value in values.items():
            entity = self.entity(key)
            call = None if entity is None else self.b.call(key, entity, value)
            if call is None:
                out[key] = Written("not_sent", None)
                continue
            try:
                running = self._call(*call)
            except Exception:  # noqa: BLE001 - refused before the service ran: no frame left
                out[key] = Written("not_sent", None)
                continue
            self.frames += 1
            try:
                await asyncio.wait_for(running, CALL_S)
            except Exception:  # noqa: BLE001 - the service ran (it reads, then writes): the frame may have left
                out[key] = Written("unknown", None)
                continue
            read = await self.read([key])
            out[key] = Written("verified", read[key].value) if key in read and read[key].value is not None else Written("unknown", None)
        return out


# ---- the mirror ----

def _role(key: str) -> dict:
    return {"key": key, "snapshot": None, "baseline": None, "held": False, "unknown": None, "attempts": 0, "firstAt": None}


def plant_day(now: float, zone: str | None) -> str:
    """The plant's local date (TZ-001): the zone the last op carried, else UTC."""
    try:
        tz = ZoneInfo(zone) if zone else timezone.utc
    except Exception:  # noqa: BLE001 - an unknown zone (or no zone data): UTC
        tz = timezone.utc
    return datetime.fromtimestamp(now, tz).date().isoformat()


def clock_check(state: str | None, last_updated: float | None, offset_s: float | None, ha_zone: str | None, plant_zone: str | None,
                tolerance_s: int, stale_s: int, now: float) -> str | None:
    """TASK-1099 (docs/28 section 7): the inverter's clock against SunnyHome's, before every program write. state: the profile's clock
    entity (an ISO time with a zone: HA's datetime entity); it is read back as the inverter's wall time through HA's zone (how the integration
    attaches a zone is unverified: a CHECK-006 precondition) and placed in the plant's zone. last_updated: when HA read it (HA's clock),
    offset_s: SunnyHome's measured offset of HA's clock (ClockOffsets). None: within tolerance; else clock_dst (about one hour off),
    clock_off, or clock_unknown (no reading, unparseable, no offset, older than stale_s)."""
    if not state or state in ("unavailable", "unknown") or last_updated is None or offset_s is None or now - last_updated > stale_s:
        return "clock_unknown"
    try:
        shown = datetime.fromisoformat(state)
        ha = ZoneInfo(ha_zone) if ha_zone else timezone.utc
        plant = ZoneInfo(plant_zone) if plant_zone else timezone.utc
        wall = (shown.astimezone(ha) if shown.tzinfo else shown).replace(tzinfo=None)  # the inverter's own wall time
        inverter = wall.replace(tzinfo=plant).timestamp()
    except Exception:  # noqa: BLE001 - unparseable or an unknown zone
        return "clock_unknown"
    off = inverter - (last_updated + offset_s)
    if abs(off) <= tolerance_s:
        return None
    if abs(abs(off) - 3600) <= tolerance_s:
        return "clock_dst"
    return "clock_off"


class Controller:
    """One device's mirror (the fixed-key store holds it by serial) and the core's per-role rules, executed on it.

    TASK-1099: two scopes, from the family's data. Program roles (the slot roles and the days key) form the day's Time of Use program; every
    other role is a lease role (unit 4). Each scope keeps its own state: the lease its active intent and until, the program the block
    d["program"] (day, base, window, pending, ending, ops, failure, clock). A lease end never touches the program and a program never ends a
    lease; letting go restores both, lease roles first. The program written is built by SunnyHome's one builder; this class only writes it.
    """

    def __init__(self, binding: Binding, io: Io, wall: Callable[[], float]) -> None:
        self.b, self.io, self.wall = binding, io, wall
        self.d: dict | None = None  # serial, instance, state, reason, active, until, roles, writes, timings, zone, drift, program
        self.last_read = 0.0
        self.last_restore = 0.0  # when the last restore ran: a restore still owed is tried again at the held read's cadence
        self.clock: Callable[[], str | None] = lambda: "clock_unknown"  # the runner's inverter clock check (DEYE-023 section 6)
        self.serial: str | None = None  # DEYE-036: the runner's store key and the inverter entry this controller writes to
        self.hub: str | None = None

    # -- store --
    def load(self, data: object, serial: str) -> None:
        self.d = data if isinstance(data, dict) and data.get("serial") == serial and isinstance(data.get("roles"), dict) else None
        p = (self.d or {}).get("program")
        if isinstance(p, dict) and p.pop("writing", None):
            p.update(ending=True, window=None)  # DEYE-030 (C3): a program write cut off (a restart, a reload): the owner's program comes back

    def dump(self) -> dict | None:
        return self.d

    def device(self, serial: str, timings: dict | None, zone: str | None) -> dict:
        if self.d is None or self.d.get("serial") != serial:
            self.d = {"serial": serial, "instance": None, "state": "idle", "reason": None, "active": None, "until": None, "roles": {},
                      "writes": {"day": None, "counts": {}}, "timings": timings, "zone": zone, "drift": None, "profile": self.b.id}
        if timings and self.d["state"] == "idle":
            self.d["timings"] = timings  # an armed lease keeps the values it was armed under
        if zone:
            self.d["zone"] = zone
        return self.d

    def held(self, scope: str | None = None) -> list[str]:
        """The held roles; scope "lease" or "program" (from the family's data), None: both."""
        return [k for k, r in (self.d or {}).get("roles", {}).items()
                if r["held"] and (scope is None or (k in self.b.program_keys) == (scope == "program"))]

    def _count(self, key: str) -> None:
        """One write of a role on the plant's day; the counts are per role, a register's budget sums its roles (docs/28 section 5.4)."""
        w, day = self.d["writes"], plant_day(self.wall(), self.d.get("zone"))
        if w["day"] != day:
            w["day"], w["counts"] = day, {}
        w["counts"][key] = w["counts"].get(key, 0) + 1

    def used(self, register: str) -> int:
        w = self.d["writes"]
        if w["day"] != plant_day(self.wall(), self.d.get("zone")):
            return 0
        return sum(n for k, n in w["counts"].items() if self.b.register_of.get(k) == register)

    def _owed(self, r: dict) -> bool:
        """A restore is owed, per scope (a program role only while the program is ending or its tries started)."""
        if r["key"] in self.b.program_keys:
            return r["attempts"] > 0 or bool(self._program().get("ending"))
        # DEYE-030 delta review #1: a rebased lease role is not owed by its lease end until a new apply (as due() and the core's handover);
        # DEYE-038 (the core's A8): a held mode role the running form does not write (let go by an apply) is owed, rebased or not: put back by
        # someone else it is restored, never drift
        until = self.d["until"]
        return r["attempts"] > 0 or not self.d.get("rebased") and (until is None or self.wall() >= until) \
            or r["key"] in self.b.mode_roles and r["key"] not in self.b.lease_roles(self.d.get("active"))

    @staticmethod
    def _own(r: dict, value) -> bool:
        return value == r["baseline"] or r["unknown"] is not None and r["unknown"] == value

    def _restored(self, r: dict) -> None:
        r.update(baseline=r["snapshot"], held=False, attempts=0, firstAt=None, unknown=None)

    def _idle(self, reason: str | None) -> None:
        """The lease is over; the device is idle only when the program holds nothing either."""
        if self.held("program") and self.d["state"] != "restore_pending":
            self.d.update(reason=reason, active=None, until=None)
            return
        self.d.update(state="idle", reason=reason, active=None, until=None)
        self.d.pop("pendingSince", None)  # DEYE-030 re-review N5: leaving restore_pending

    async def _write(self, values: dict[str, str]) -> dict[str, Written]:
        """DEYE-030 (C3): each value recorded as SunnyHome's (unknown) before the frame, so a write cut off mid-way is still known as ours;
        re-review N4: when nothing left (cut off before the first frame), the records go again, so an owner's equal value is never taken as
        SunnyHome's."""
        roles = self.d["roles"]
        prior = {k: roles[k]["unknown"] for k in values}
        for k, v in values.items():
            roles[k]["unknown"] = v
        frames = self.io.frames
        try:
            out = await self.io.write(values)
        except BaseException:
            if self.io.frames == frames:
                for k, v in prior.items():
                    roles[k]["unknown"] = v
            raise
        for k, w in out.items():
            if w.sent == "not_sent":
                roles[k]["unknown"] = prior[k]
        return out

    # -- the plant's clock --
    def _local(self) -> datetime:
        zone = (self.d or {}).get("zone")
        try:
            tz = ZoneInfo(zone) if zone else timezone.utc
        except Exception:  # noqa: BLE001 - an unknown zone: UTC
            tz = timezone.utc
        return datetime.fromtimestamp(self.wall(), tz)

    def _program(self) -> dict:
        if self.d is None:
            return {}
        p = self.d.get("program")
        if not isinstance(p, dict):
            p = self.d["program"] = {"day": None, "base": None, "window": None, "pending": None, "ending": False,
                                     "ops": {"day": None, "counts": {}}, "failure": None, "clock": None, "guardTries": 0}
        return p

    # -- the core's steps (the lease scope) --
    async def apply(self, active: str, targets: dict[str, str], until: float) -> tuple[str | None, int]:
        """Opens or extends the lease; (reason, writes). Callers checked the option, the backup answer, the serial and the definition."""
        d = self.d
        if d["state"] == "restore_pending":
            return "restore_pending", 0
        if any(k not in self.b.register_of or k in self.b.program_keys or not self.b.encodable(self.io.definition, self.io.variant, k, v)
               for k, v in targets.items()):
            return "invalid_command", 0
        roles, tries = d["roles"], d["timings"]["restoreTryMinutes"]
        # DEYE-030 (docs/28 section 12, the core's SettingsControl.apply): from stored state only. A mode form's apply lets go the held mode
        # roles it does not write; any apply retries a held role outside its targets whose restore is owed (a failed exit, a lapsed lease, a
        # counted try) and a held mode role the running mode form does not write. Another function's role (the export limit beside a hold)
        # stays held. A retry that was not sent is owed again on every later apply, within the restore tries.
        modes, running, held = self.b.mode_roles, self.b.lease_roles(d.get("active")), self.held("lease")
        extra = sorted({k for k in held if k not in targets and (
            modes & set(targets) and k in modes or self._owed(roles[k]) or k in modes and k not in running)})
        reads = await self.io.read(list(targets) + extra)
        if not all(k in reads for k in targets):
            return "unreadable", 0
        for k in list(extra):  # spent tries while the lease renews: the fresh read decides
            r = roles[k]
            if r["attempts"] < len(tries) or k not in reads:
                continue
            if reads[k].value == r["snapshot"]:
                self._restored(r)
            elif not self._own(r, reads[k].value):
                r["held"] = False
            else:  # still SunnyHome's value: the owner is told and applies are refused until it resolves (owner decision 2026-11-25)
                d.update(state="restore_pending", pendingSince=self.wall(), reason="restore_refused", active=None, until=None)
                return "restore_pending", 0
            extra.remove(k)
        for k in targets:
            r = roles.get(k)
            if (r is None or not r["held"]) and not reads[k].valid:
                return "snapshot_invalid", 0
        delta = {k: v for k, v in targets.items() if v != reads[k].value}
        for k in targets:
            r, value = roles.get(k), reads[k].value
            if r and r["held"] and not self._own(r, value) and not (self._owed(r) and value == r["snapshot"]) and targets[k] != value:
                writes = await self.drift([k])
                return "owner_changed", writes
        dropped = [k for k in extra if k in reads and reads[k].value != roles[k]["snapshot"]]  # the restores this apply may write
        cost: dict[str, int] = {}
        for k in list(delta) + dropped:
            cost[self.b.register_of[k]] = cost.get(self.b.register_of[k], 0) + 1
        if any(self.used(reg) + n > self.b.capability("writeBudgetPerRegisterPerDay") for reg, n in cost.items()):
            return "budget_exhausted", 0  # the component's hard stop, applies only
        if self._program().get("window"):  # DEYE-030 (C6): after every check that refuses, so a refusal never wrote the window's end
            await self.end_window()  # DEYE-023 pre review #6: a lease ends an open window first
            if self._program().get("window") or d["state"] == "restore_pending":
                return "busy", 0
        restored = 0
        if extra:
            _, restored = await self._restore_keys(extra, reads)  # at the snapshot restored, the owner's value let go, else at its try times
        if d["state"] == "idle":
            d["instance"], d["reason"] = str(uuid.uuid4()), None
        for k in targets:
            r = roles.setdefault(k, _role(k))
            value = reads[k].value
            if not r["held"]:
                r.update(snapshot=value, baseline=value, held=True, unknown=None)
            if r["unknown"] is not None and r["unknown"] == value:
                r.update(baseline=value, unknown=None)
            elif self._owed(r) and value == r["snapshot"]:
                r["baseline"] = value  # SETCTL-001 deferred P3: an exit started and the role reads at the snapshot
            r.update(attempts=0, firstAt=None)
        # a lapsed or exited lease: its mode has ended (a rebased one still runs: the core's handover keeps it)
        ended = not d.get("rebased") and (d["until"] is None or self.wall() >= d["until"])
        d.pop("rebased", None)  # DEYE-030 (C2): a new lease is owed its restores again
        if ended or self.b.is_mode(active) or d.get("active") is None or not self.b.is_mode(d["active"]):
            d["active"] = active  # a function's apply (the export limit) never replaces a running mode
        d.update(state="armed", until=until)
        if not delta:
            return None, restored
        sent, worst = restored, None
        for k, w in (await self._write(delta)).items():
            r = roles[k]
            if w.sent == "verified":
                r.update(baseline=w.value, unknown=None)
                d["verified"] = self.wall()
                self._count(k)
                sent += 1
                if w.value != delta[k] and worst in (None, "not_sent"):
                    worst = "not_applied"  # DEYE-030 (C7): the register kept or clamped another value
            elif w.sent == "unknown":
                self._count(k)
                sent += 1
                worst = "unknown"
            else:
                worst = worst or "not_sent"
        return worst, sent

    async def _restore_keys(self, keys: list[str], reads: dict | None = None) -> tuple[bool, int]:
        """The per-role restore (the core's rule) of these held roles; (someone else changed one, writes). No state change of the device."""
        tries = self.d["timings"]["restoreTryMinutes"]
        roles, now = self.d["roles"], self.wall()
        if reads is None:
            reads = await self.io.read(keys) if keys else {}
        changed, due = False, {}
        for k in keys:
            r = roles[k]
            if r["attempts"] >= len(tries) or k not in reads:
                continue
            value = reads[k].value
            if value == r["snapshot"]:
                self._restored(r)
                continue
            if not self._own(r, value):
                r["held"], changed = False, True
                continue
            if r["attempts"] > 0 and now < r["firstAt"] + tries[r["attempts"]] * 60:
                continue
            due[k] = r["snapshot"]
        sent = 0
        for k, w in (await self.io.write(due) if due else {}).items():
            r = roles[k]
            if w.sent == "not_sent":
                continue
            sent += 1
            self._count(k)
            if r["attempts"] == 0:
                r["firstAt"] = now
            r["attempts"] += 1
            if w.sent == "verified":
                if w.value == r["snapshot"]:
                    self._restored(r)
                else:
                    r.update(baseline=w.value, unknown=None)  # a clamped restore is the new baseline
        return changed, sent

    def _settle(self, changed: bool) -> str | None:
        d, tries = self.d, self.d["timings"]["restoreTryMinutes"]
        refused = any(r["held"] and r["attempts"] >= len(tries) for r in d["roles"].values())
        if refused:
            if d["state"] != "restore_pending":
                d.update(state="restore_pending", pendingSince=self.wall())
            d["reason"] = "restore_refused"
        elif not self.held():
            self._idle("owner_changed" if changed else None)
        elif changed:
            d["reason"] = "owner_changed"
        return "restore_refused" if refused else "owner_changed" if changed else None

    async def restore(self, scope: str | None = None) -> tuple[str | None, int]:
        """The exit / lease-end / letting-go restore, role by role; never refused for the budget, the clock or the option. scope None (letting
        go) restores the lease roles first, then the program in the day-off order; "lease" or "program" only that scope."""
        d = self.d
        if d is None or d["state"] == "idle":
            return None, 0
        self.last_restore = self.wall()
        changed, sent = False, 0
        if scope in (None, "lease"):
            d["active"], d["until"] = None, None
            d.pop("rebased", None)  # an exit or letting go after a rebase is owed its tries like any other (delta review #3)
            c, n = await self._restore_keys(self.held("lease"))
            changed, sent = changed or c, sent + n
        if scope in (None, "program") and self.held("program"):
            p = self._program()
            p.update(ending=True, window=None, pending=None)
            if not self.held("lease"):  # lease roles first: work_mode / export_limit come back before any day-off state
                c, n = await self._restore_program()
                changed, sent = changed or c, sent + n
        elif scope in (None, "program") and self.d.get("program"):
            self._program().update(ending=False, window=None, pending=None, day=None, base=None)
        return self._settle(changed), sent

    async def _restore_program(self) -> tuple[bool, int]:
        """The owner's program back in the day-off order: the days key off (when ours and running today), the slot roles, the days key last."""
        roles, p, days = self.d["roles"], self._program(), self.b.days_key
        keys = self.held("program")
        reads = await self.io.read(keys)
        iso = self._local().isoweekday()
        if days in keys and days in reads:
            r, value = roles[days], reads[days].value
            if value != r["snapshot"] and not self._own(r, value) and self.b.runs_on(value, iso):
                self._leave_program(keys, reads)  # pre review #5: the owner runs a program of their own today: leave every program role as it is
                return True, 0
        slots, sent = [k for k in keys if k != days], 0
        need = [k for k in slots if k in reads and reads[k].value != roles[k]["snapshot"] and self._own(roles[k], reads[k].value)]
        if days in keys and days in reads and need:
            r, value = roles[days], reads[days].value
            if self._own(r, value) and self.b.runs_on(value, iso) and value != self.b.no_day():
                # post review #2: the day off is a counted try of the days role (the restore try schedule), so it is bounded
                tries, now = self.d["timings"]["restoreTryMinutes"], self.wall()
                if r["attempts"] >= len(tries) or r["attempts"] > 0 and now < r["firstAt"] + tries[r["attempts"]] * 60:
                    return False, 0
                ok, n = await self._step({days: self.b.no_day()})
                if not ok:
                    if n:
                        r["firstAt"] = r["firstAt"] or now
                        r["attempts"] += 1
                    return False, n  # stays owed: the next try at its time, restore_pending after the last
                r.update(attempts=0, firstAt=None)
                sent += n
        changed, n = await self._restore_keys(slots, reads)
        sent += n
        if days in keys and not any(roles[k]["held"] for k in slots):
            c, n = await self._restore_keys([days])
            changed, sent = changed or c, sent + n
        if not self.held("program"):
            p.update(ending=False, day=None, base=None)
        return changed, sent

    async def _step(self, values: dict[str, str]) -> tuple[bool, int]:
        """One step of a program write: every value written and read back as written; (all verified, writes)."""
        roles, ok, sent = self.d["roles"], True, 0
        for k, w in (await self._write(values)).items():
            r = roles[k]
            if w.sent == "not_sent":
                ok = False
                continue
            sent += 1
            self._count(k)
            if w.sent == "verified":
                r.update(baseline=w.value, unknown=None)
                ok = ok and w.value == values[k]
            else:
                r["unknown"], ok = values[k], False
        return ok, sent

    async def check(self) -> tuple[str | None, int]:
        """The held fresh read (every readS): drift ends that scope's mode, a pending role at its snapshot is restored; programRequires is
        read too while a program is held (delta pre review N4)."""
        d = self.d
        self.last_read = self.wall()
        if d is None or not self.held():
            return None, 0
        roles = d["roles"]
        if d["state"] == "restore_pending":
            held = self.held()
            reads = await self.io.read(held)
            elsewhere = False
            for k in held:
                if k not in reads:
                    continue
                r = roles[k]
                if reads[k].value == r["snapshot"]:
                    self._restored(r)
                elif not self._own(r, reads[k].value):
                    elsewhere = True
            if not self.held():
                self._idle(None)
                d.pop("pendingSince", None)
                return None, 0
            d["reason"] = "changed_elsewhere" if elsewhere else "restore_refused"
            return d["reason"], 0
        lease, program = self.held("lease"), self.held("program")
        p = self._program() if program else {}
        rules = self.b.family.get("programRequires", {}) if program and not p.get("ending") else {}
        reads = await self.io.read(lease + program + [k for k in rules if k not in lease])
        if any(k in reads and reads[k].value in rule.get("notIn", []) and not (k in lease and self._own(roles[k], reads[k].value))
               for k, rule in rules.items()):  # DEYE-030 (Low): the lease's own value of such a role is not the owner's move
            p["failure"] = "program_unsafe"  # the owner moved to a mode under which self-use slots sell the battery: the program ends
            _, writes = await self.restore("program")
            return "program_unsafe", writes
        drifted, drifted_program = [], []
        for k in lease + program:
            if k not in reads:
                continue
            r, value = roles[k], reads[k].value
            if value == r["baseline"]:
                continue
            if self._owed(r) and value == r["snapshot"]:
                self._restored(r)
                continue
            if r["unknown"] is not None and r["unknown"] == value:
                r.update(baseline=value, unknown=None)
                continue
            (drifted_program if k in self.b.program_keys else drifted).append(k)
        writes = 0
        days = self.b.days_key
        if days in drifted_program and self.b.runs_on(reads[days].value, self._local().isoweekday()):
            self._leave_program(program, reads)  # pre review #5: the owner runs a program of their own today: every program role is left as it is
            self._program().update(window=None, pending=None)
            self._drift_count()
            drifted_program = []
            if not self.held():
                self._idle("owner_changed")
            else:
                d["reason"] = "owner_changed"
            if not drifted:
                return "owner_changed", 0
        if drifted_program:
            for k in drifted_program:
                roles[k]["held"] = False  # left as the owner set it
            self._drift_count()
            _, writes = await self.restore("program")
            if d["state"] != "restore_pending":
                d["reason"] = "owner_changed"
        if drifted:
            writes += await self.drift(drifted)
        if drifted or drifted_program:
            return "owner_changed", writes
        if all(k in reads for k in lease + program):
            d["verified"] = self.wall()
        if not self.held():
            self._idle(None)
        return None, 0

    def _leave_program(self, keys: list[str], reads: dict) -> None:
        """The owner runs a program of their own today: every program role is left as it is. DEYE-030 (C11): SunnyHome's slots may stay in it,
        so the owner's own values of the roles that differ are kept in "left" for the runner to show once."""
        roles = self.d["roles"]
        left = {k: roles[k]["snapshot"] for k in keys if k != self.b.days_key and k in reads and reads[k].value != roles[k]["snapshot"]
                and self._own(roles[k], reads[k].value)}
        for k in keys:
            roles[k]["held"] = False
        self._program().update(ending=False, day=None, base=None, left=left or None)

    def _drift_count(self) -> None:
        d, day = self.d, plant_day(self.wall(), self.d.get("zone"))
        drift = d.get("drift") if isinstance(d.get("drift"), dict) and d["drift"].get("day") == day else {"day": day, "count": 0}
        d["drift"] = {"day": day, "count": drift["count"] + 1}  # the day's drifts: the backend takes the count, never adds to it

    async def drift(self, keys: list[str]) -> int:
        """An owner override of lease roles: those roles are left as set, the lease ends, its other roles are restored."""
        d = self.d
        for k in keys:
            d["roles"][k]["held"] = False
        d["active"], d["until"] = None, None
        self._drift_count()
        _, writes = await self.restore("lease")
        if d["state"] != "restore_pending":
            d["reason"] = "owner_changed"
            if not self.held():
                self._idle("owner_changed")
        return writes

    # -- the program scope (TASK-1099, docs/28 section 7) --
    def _discharge_active(self) -> bool:
        """A held lease whose form also sets the other programs (discharge under Export First): no day-off state may run."""
        active = (self.d or {}).get("active")
        return bool(active and self.held("lease") and any(f.get("otherPrograms") for f in self.b.family["intents"].get(active, [])))

    def _ops_today(self) -> dict:
        p, day = self._program(), plant_day(self.wall(), self.d.get("zone"))
        if p["ops"].get("day") != day:
            p["ops"] = {"day": day, "counts": {}}
        return p["ops"]["counts"]

    def _targets(self, slots: list[dict], own: dict[str, str | None]) -> dict[str, str] | None:
        """The role values of a slot list; a null field is the highest value of that field among the owner's own programs (delta review N3).
        own: the owner's value of each program role (the snapshot while held, else the fresh read)."""
        out = {}
        if not isinstance(slots, list) or len(slots) != len(self.b.slot_roles):
            return None
        for i, slot in enumerate(slots):
            if not isinstance(slot, dict):
                return None
            for field, value in slot.items():
                key = self.b.slot_roles[i].get(field)
                if key is None:
                    return None
                if value is None:
                    owned = []
                    for s in self.b.slot_roles:
                        try:
                            owned.append(float(own[s[field]]))
                        except (KeyError, TypeError, ValueError):
                            pass
                    if not owned:
                        return None
                    top = max(owned)
                    value = str(int(top)) if top.is_integer() else str(top)
                out[key] = str(value)
        return out

    def _program_refusal(self, safety: bool, lease: bool = True) -> str | None:
        """The refusals of a program write that read nothing: pending, an owed restore of the owner's program (DEYE-030 C4: never a write over
        it), a held discharge (lease False: the caller ends the lease first) and the clock (a safety write: only the first two)."""
        p = self._program()
        if self.d["state"] == "restore_pending":
            return "restore_pending"
        if p.get("ending"):
            return "busy"
        if safety:
            return None
        if lease and self._discharge_active():
            return "discharge_active"
        p["clock"] = self.clock()
        return p["clock"]

    async def write_program(self, day: str, slots: list[dict], op: str | None, safety: bool = False) -> tuple[str | None, int]:
        """Writes one day's program (a slot list from SunnyHome's builder) for the plant's today. Refused before any write while pending, while
        the owner's program is owed back, while discharge holds Export First, by the clock, by programRequires, by a value the register cannot
        hold, by maxPerDay and by the budget (a safety write: only the first two and the value). DEYE-030 (C1): every program role is snapshotted
        only after every refusal passed. Order: the days key off when it runs today, the changed slot roles, the today-only value; a step that
        does not verify stops with the day off, never a mixed program. op None: "rewrite" over a running day, else "midnight"."""
        d, p = self.d, self._program()
        if (reason := self._program_refusal(safety)) is not None:
            return reason, 0
        keys = sorted(self.b.program_keys)
        rules = {} if safety else self.b.family.get("programRequires", {})
        reads = await self.io.read(keys + [k for k in rules if k not in keys])
        if any(k not in reads for k in list(keys) + list(rules)):
            return "unreadable", 0
        if any(reads[k].value in rule.get("notIn", []) for k, rule in rules.items()):
            return "program_unsafe", 0
        roles, days = d["roles"], self.b.days_key
        first = not self.held("program")
        if first:
            # the owner's program, before SunnyHome's first program write; a days value that does not round-trip is never a snapshot
            if any(not reads[k].valid for k in keys) or reads[days].value not in self.b.day_values:
                return "snapshot_invalid", 0
        else:
            for k in keys:
                r = roles.get(k)
                if r and r["held"] and r["unknown"] is not None and r["unknown"] == reads[k].value:
                    r.update(baseline=reads[k].value, unknown=None)
                if r and r["held"] and not self._own(r, reads[k].value):
                    return "changed_elsewhere", 0  # someone else's value: the held read ends the program as owner_changed
        own = {k: roles[k]["snapshot"] if not first and roles.get(k, {}).get("held") else reads[k].value for k in keys}
        targets = self._targets(slots, own)
        if targets is None or any(not self.b.encodable(self.io.definition, self.io.variant, k, v) for k, v in targets.items()):
            return "invalid_command", 0  # DEYE-030 (C8, Low): a slot list the registers cannot hold, before the day-off step
        if not first and any(not roles.get(k, {}).get("held") and (k == days or targets.get(k, reads[k].value) != reads[k].value) for k in keys):
            return "changed_elsewhere", 0  # DEYE-030 (C4): a program role the owner took back is never written over
        iso = datetime.fromisoformat(day).isoweekday()
        running = self.b.runs_on(reads[days].value, iso) and reads[days].value != self.b.no_day()
        op = op or ("rewrite" if running else "midnight")
        today_only = self.b.only_on(iso)
        delta = {k: v for k, v in targets.items() if v != reads[k].value}
        starts = {slot.get("start") for slot in self.b.slot_roles}
        if op == "extension" and any(k not in starts for k in delta):
            op = "rewrite"  # post review #6: only start moves are an extension (safe in either state); anything else takes the full order
        off = bool(delta) and running and op != "extension"
        last = off or reads[days].value != today_only
        if not safety:
            cap = self.b.family.get("maxPerDay", {}).get(op)
            if cap is not None and self._ops_today().get(op, 0) >= cap:
                return "budget_exhausted", 0
            cost: dict[str, int] = {}
            for k in list(delta) + [days] * (int(off) + int(last)):
                cost[self.b.register_of[k]] = cost.get(self.b.register_of[k], 0) + 1
            if any(self.used(reg) + n > self.b.capability("writeBudgetPerRegisterPerDay") for reg, n in cost.items()):
                return "budget_exhausted", 0
        if first:
            if d["state"] == "idle":
                d["instance"], d["reason"] = str(uuid.uuid4()), None
            for k in keys:
                r = roles.setdefault(k, _role(k))
                r.update(snapshot=reads[k].value, baseline=reads[k].value, held=True, unknown=None, attempts=0, firstAt=None)
            d["state"] = "armed"
            p.update(ending=False, guardTries=0, guardFirstAt=None)
        sent, ok = 0, True
        p["writing"] = True  # DEYE-030 (C3): a write cut off before its end brings the owner's program back at the next load
        if off:
            ok, n = await self._step({days: self.b.no_day()})  # 1. the day off
            sent += n
        if ok and delta:
            ok, n = await self._step(delta)  # 2. the slots
            sent += n
        if ok and last:
            ok, n = await self._step({days: today_only})  # 3. today only
            sent += n
        p.pop("writing", None)
        if sent:
            self._ops_today()[op] = self._ops_today().get(op, 0) + 1
            d["verified"] = self.wall()
        if not ok:
            p["failure"] = "schedule_midnight_failed" if op == "midnight" else "schedule_failed"
            if not p.get("base"):
                p["ending"] = True  # no day's program to fall back on: the owner's program comes back at the held read's cadence
            return "not_applied", sent
        p.update(day=day, failure=None, ending=False)
        return None, sent

    async def window(self, program: dict, until: float, kind: str) -> tuple[str | None, int]:
        """A device-form apply's window op (insert, extension or refresh), built by SunnyHome on what program["from"] names. DEYE-030 (C6):
        every refusal that does not depend on the lease comes before the held lease is ended."""
        d, p = self.d, self._program()
        today = plant_day(self.wall(), d.get("zone"))
        if program.get("day") != today:
            return "invalid_command", 0
        held_base = p.get("base") if p.get("day") == today else None
        win = p.get("window") if p.get("day") == today else None
        held_window = {"start": win["start"], "end": win["end"]} if win else None
        since = program.get("from") or {}
        if since.get("base") != held_base or since.get("window") != held_window:
            return "changed_elsewhere", 0  # built on a base or window this mirror no longer holds (delta pre review N2)
        w = program.get("window") or {}
        if program.get("op") == "refresh":
            if win is None:
                return "changed_elsewhere", 0
            win["until"] = until
            return None, 0
        requires = program.get("requires") or {}
        for k, v in requires.items():  # read, never written: the polled state (such a setting has no register binding of its own)
            entity = self.io.entity(k)
            if entity is None or self.b.polled(k, self.io.state(entity)) != v:
                return "requires_unmet", 0
        if (reason := self._program_refusal(False, lease=False)) is not None:
            return reason, 0
        if self.held("lease"):
            await self.restore("lease")  # pre review #6: a window ends a held lease first
            if self.held("lease"):
                return "busy", 0
        reason, sent = await self.write_program(today, program.get("slots") or [], program.get("op"))
        if reason is None:
            p["window"] = {"start": w.get("start"), "end": w.get("end"), "until": until, "kind": kind}
        return reason, sent

    async def window_ended(self) -> tuple[str | None, int]:
        """A window's end time passed: the inverter ended it at the slot boundary. With a day's program it is only cleared; without one the
        owner's program comes back (pre review #1)."""
        p = self._program()
        if p.get("base") and p.get("day") == plant_day(self.wall(), self.d.get("zone")):
            p["window"] = None
            return None, 0
        return await self.end_window()

    async def end_window(self) -> tuple[str | None, int]:
        """A window's lapse, early stop or end: back to the day's base program when one is held today, else the owner's program. DEYE-030 (C9):
        a slot restore that does not land brings the owner's program back (bounded tries, then restore_pending), never a retry loop."""
        p = self._program()
        p["window"] = None
        if p.get("base") and p.get("day") == plant_day(self.wall(), self.d.get("zone")):
            reason, sent = await self.write_program(p["day"], p["base"], "slotRestore", safety=True)
            if reason is None:
                return None, sent
            fallback, n = await self.restore("program")
            return fallback or reason, sent + n
        return await self.restore("program")

    async def schedule(self, day: str, slots: list[dict]) -> tuple[str | None, int]:
        """A day's program from SunnyHome (unit 6 sends it): a later day waits as pending for the midnight write, today is written now."""
        p, today = self._program(), plant_day(self.wall(), self.d.get("zone"))
        if day > today:
            p["pending"] = {"day": day, "slots": slots}
            return None, 0
        if day < today:
            return "invalid_command", 0
        reason, sent = await self.write_program(day, slots, None)
        if reason is None:
            p.update(base=slots, window=None)
        return reason, sent

    async def midnight(self) -> tuple[str | None, int]:
        """The pending program of today, once; a failed one is not retried (no repeat), a passed one is dropped. DEYE-030 (Low): priced as the
        midnight write even over a running owner's day (the day-off order still applies), so it never uses the day's one rewrite."""
        p, today = self._program(), plant_day(self.wall(), self.d.get("zone"))
        pending, p["pending"] = p.get("pending"), None
        if not pending or pending["day"] != today:
            return None, 0
        reason, sent = await self.write_program(today, pending["slots"], "midnight")
        if reason is None:
            p.update(base=pending["slots"], window=None)
        return reason, sent

    async def repeat_guard(self) -> tuple[str | None, int]:
        """A SunnyHome program of an earlier day whose days value runs today again (a week later): the day off. A safety write, paced and
        bounded by the restore tries (DEYE-030 C9); after the last one the device is restore_pending, so the owner is told."""
        p, days, roles = self._program(), self.b.days_key, self.d["roles"]
        tries, now = self.d["timings"]["restoreTryMinutes"], self.wall()
        self.last_read = now
        reads = await self.io.read([days])
        if days not in reads:
            return None, 0  # unreachable: asked again at the held read's cadence
        if not self._own(roles[days], reads[days].value):
            return await self.check()  # the owner's days value: the held read's drift rule decides
        if not p.get("guardTries"):
            p["guardFirstAt"] = now
        p["guardTries"] = p.get("guardTries", 0) + 1
        ok, sent = await self._step({days: self.b.no_day()})
        if ok:
            p.update(guardTries=0, guardFirstAt=None)
        elif p["guardTries"] >= len(tries):
            p["failure"] = "repeat_guard_failed"
            self.d.update(state="restore_pending", reason="restore_refused", pendingSince=now)
        return (None if ok else "not_applied"), sent

    def _guard_due(self) -> bool:
        p, days = self._program(), self.b.days_key
        r = (self.d or {}).get("roles", {}).get(days)
        today = plant_day(self.wall(), self.d.get("zone"))
        if not r or not r["held"] or p.get("ending") or not p.get("day") or p.get("day") == today or (p.get("pending") or {}).get("day") == today:
            return False  # delta pre review N1: never over today's own program; DEYE-030 (C1): only over a program SunnyHome wrote (a held day)
        t, n, now = self.d["timings"], p.get("guardTries", 0), self.wall()
        if n >= len(t["restoreTryMinutes"]) or now < self.last_read + t["readS"] or n and now < (p.get("guardFirstAt") or 0) + t["restoreTryMinutes"][n] * 60:
            return False  # DEYE-030 (C9): at the held read's cadence and the restore try times, never every tick
        return r["baseline"] != self.b.no_day() and self.b.runs_on(r["baseline"], self._local().isoweekday())

    # -- ops --
    def clear(self, instance: str) -> bool:
        """The OWNER's clear_restore, accepted by the core and bound to the pending instance."""
        d = self.d
        if d is None or d["state"] != "restore_pending" or d["instance"] != instance:
            return False
        for r in d["roles"].values():
            r.update(held=False, attempts=0, firstAt=None, unknown=None)
        d.pop("program", None)
        self._idle(None)
        d.pop("pendingSince", None)
        return True

    def release(self, instance: str) -> bool:
        """Handover: the lease and the mirror go, with no restore (the new holder restores)."""
        if self.d is None or self.d["instance"] != instance:
            return False
        self.d = None
        return True

    def rebase(self, instance: str, baselines: dict[str, str]) -> bool:
        """A returning component: the new baselines, no lease (it restores nothing until a new apply)."""
        d = self.d
        if d is None or d["instance"] != instance:
            return False
        for k, r in d["roles"].items():
            if k in baselines:
                r.update(baseline=baselines[k], held=True, unknown=None)
            else:
                r["held"] = False
        # the running mode stays while a lease role is held, as the core's handover
        d.update(until=None, state="armed" if self.held() else "idle", rebased=True, active=d["active"] if set(self.held("lease")) & self.b.lease_roles(d["active"]) else None)
        return True

    def due(self) -> str | None:
        """What the tick owes now: "restore" (a lease end past its grace, a counted try, an ending program), "window_lapse", "window_end",
        "midnight", "repeat_guard", "check" (the held read), or None."""
        d = self.d
        if d is None or not d.get("timings"):
            return None
        t, now = d["timings"], self.wall()
        if d["state"] == "idle":  # post review #3: a next-day program waits on an idle device too
            pending = (d.get("program") or {}).get("pending")
            return "midnight" if pending and self._pending_due(pending) else None
        if d["state"] == "armed" and d["until"] is not None and now >= d["until"] + t["graceS"]:
            return "restore"
        if d["state"] == "restore_pending":  # DEYE-030 re-review N2 (the core's S8): a role with tries left is still tried at its times
            for k, r in d["roles"].items():
                if r["held"] and r["attempts"] < len(t["restoreTryMinutes"]) and now >= self.last_restore + t["readS"] and (
                        not r["attempts"] or now >= r["firstAt"] + t["restoreTryMinutes"][r["attempts"]] * 60):
                    return "restore_program" if k in self.b.program_keys else "restore"
        tries = t["restoreTryMinutes"]
        if d["state"] == "armed":
            p = self._program() if d.get("program") else {}
            for k, r in d["roles"].items():
                program = k in self.b.program_keys
                if not r["held"] or (not p.get("ending") if program else d["until"] is not None or d.get("rebased")):
                    continue  # DEYE-030 (C2): a rebased lease role restores nothing until a new apply; nothing else waits on it
                scope = "restore_program" if program else "restore"  # post review #1: the owed scope ("restore" is the lease's, as in unit 4)
                if 0 < r["attempts"] < len(tries) and now >= r["firstAt"] + tries[r["attempts"]] * 60:
                    return scope
                if r["attempts"] == 0 and now >= self.last_restore + t["readS"]:
                    return scope  # an ended lease or program whose role was never read (unreachable, a restart): still owed
            w = p.get("window")
            if w and now >= w["until"] + t["graceS"]:
                return "window_lapse"
            local = self._local()
            if w and (p.get("day") != local.date().isoformat() or local.hour * 60 + local.minute >= w["end"]):
                return "window_end"
            if p.get("pending") and self._pending_due(p["pending"]):
                return "midnight"
            if p and self._guard_due():
                return "repeat_guard"
        return "check" if self.held() and now >= self.last_read + t["readS"] else None

    def _pending_due(self, pending: dict) -> bool:
        """After 00:00 plus the clock tolerance of the pending day (an inverter clock still in the old day never runs it early), or passed."""
        local = self._local()
        today = local.date().isoformat()
        return pending["day"] < today or pending["day"] == today and (
            local.hour * 3600 + local.minute * 60 + local.second >= self.b.capability("clockToleranceSeconds"))

    def known(self, serial: str | None, timings: dict | None, zone: str | None) -> dict | None:
        """The block to report: an idle device as soon as its serial and the server's timings are known (so SunnyHome can take the first apply)."""
        if self.d is None and serial is not None and timings is not None:
            self.device(serial, timings, zone)
        return self.report()

    def report(self) -> dict | None:
        """The brand-free settings.control block (the backend adds the controller from the link); None before the device is known."""
        d = self.d
        if d is None:
            return None
        block = {
            "profile": d["profile"], "state": d["state"], "reason": d["reason"], "instance": d["instance"], "active": d["active"],
            "until": d["until"], "verifiedAt": d.get("verified"), "pendingSince": d.get("pendingSince"), "drift": d.get("drift") or {"day": None, "count": 0},
            "definition": self.io.definition, "variant": self.io.variant,
            "roles": {k: {"snapshot": r["snapshot"], "baseline": r["baseline"], "held": r["held"], "unknown": r["unknown"],
                          "attempts": r["attempts"], "firstAttemptAt": r["firstAt"]} for k, r in d["roles"].items()},
            "writes": d["writes"],
        }
        p = d.get("program")
        if isinstance(p, dict):
            w = p.get("window")
            block["program"] = {"day": p.get("day"), "base": p.get("base"), "window": {"start": w["start"], "end": w["end"]} if w else None,
                                "pending": (p.get("pending") or {}).get("day"), "ending": bool(p.get("ending")), "ops": p.get("ops"),
                                "failure": p.get("failure"), "clock": p.get("clock")}
        return block

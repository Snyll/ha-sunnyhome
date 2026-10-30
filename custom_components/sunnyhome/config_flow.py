"""Config flow: pair this Home Assistant with a SunnyHome account using a one-time code."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_CODE, CONF_TOKEN, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import (
    binary_shareable, device_choices, heat_controllable, heat_shareable, other_wallbox_ids, payload, person_shareable, sensor_shareable, shareable, solax_charger,
    solax_remote, unshared_entries,
)
from .const import (
    CONF_BINARY, CONF_CONTROL, CONF_DEVICES, CONF_ENTITIES, CONF_HEAT, CONF_INVERTER, CONF_LINK_ID, CONF_OTHER_WALLBOX, CONF_OW_CURRENT, CONF_OW_ENERGY, CONF_OW_POWER,
    CONF_OW_STATUS, CONF_OW_SWITCH, CONF_PEOPLE, CONF_SENSORS, CONF_WALLBOX, DEFAULT_URL, DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
_PAIR_ERRORS = {401: "pairing_invalid", 409: "link_limit"}
USER_SCHEMA = vol.Schema({vol.Required(CONF_URL, default=DEFAULT_URL): str, vol.Required(CONF_CODE): str})
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})
# WALLBOX-005: the Other wallbox step's fields, with the entity selector that offers each its own kind of entity
OTHER_WALLBOX_FIELDS = {
    CONF_OW_POWER: EntitySelectorConfig(domain="sensor", device_class="power"),
    CONF_OW_CURRENT: EntitySelectorConfig(domain="number"),
    CONF_OW_SWITCH: EntitySelectorConfig(domain="switch"),
    CONF_OW_ENERGY: EntitySelectorConfig(domain="sensor", device_class="energy"),
    CONF_OW_STATUS: EntitySelectorConfig(domain="sensor"),
}


class SunnyHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair with SunnyHome; reauth pairs again with a new code."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SunnyHomeOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            if not payload.valid_base_url(url):
                errors[CONF_URL] = "invalid_url"
            elif paired := await self._pair(url, user_input[CONF_CODE], errors):
                link_id, token = paired
                await self.async_set_unique_id(link_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="SunnyHome", data={CONF_URL: url, CONF_LINK_ID: link_id, CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input), errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            if paired := await self._pair(entry.data[CONF_URL], user_input[CONF_CODE], errors):
                link_id, token = paired
                # The entry's update listener reloads it (async_update_reload_and_abort refuses update listeners).
                self.hass.config_entries.async_update_entry(
                    entry, unique_id=link_id, data={**entry.data, CONF_LINK_ID: link_id, CONF_TOKEN: token}
                )
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(step_id="reauth_confirm", data_schema=CODE_SCHEMA, errors=errors)

    async def _pair(self, url: str, raw_code: str, errors: dict[str, str]) -> tuple[str, str] | None:
        """POST /api/ha/v1/pair; return (link_id, token) or fill errors. Never logs the code or token."""
        code = payload.normalize_code(raw_code)
        if code is None:
            errors[CONF_CODE] = "invalid_code"
            return None
        name = "".join(c for c in self.hass.config.location_name if c.isprintable())[:64] or "Home Assistant"
        try:
            async with async_get_clientsession(self.hass).post(
                f"{url}/api/ha/v1/pair",
                json={"code": code, "instanceName": name},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    return str(body["linkId"]), str(body["token"])
                _LOGGER.debug("Pairing answered HTTP %s", resp.status)
                errors["base"] = _PAIR_ERRORS.get(resp.status, "cannot_connect")
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as err:
            _LOGGER.debug("Pairing failed: %s", type(err).__name__)
            errors["base"] = "cannot_connect"
        return None


class SunnyHomeOptionsFlow(OptionsFlow):
    """Choose the switches and lights shared with SunnyHome, the subset it may control, the sensors it may read and (HA-012) the binary sensors
    and people it may see and (HA-013) the heat pumps and boilers it may read; none by default. HA-021: whole devices, shared with their control (D2)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = user_input.get(CONF_ENTITIES, [])
            control = user_input.get(CONF_CONTROL, [])
            sensors = user_input.get(CONF_SENSORS, [])
            binary = user_input.get(CONF_BINARY, [])
            people = user_input.get(CONF_PEOPLE, [])
            heat = user_input.get(CONF_HEAT, [])
            devices = user_input.get(CONF_DEVICES, [])
            choices = {device_id: ents for device_id, _, ents in device_choices(self.hass, other_wallbox_ids(self.config_entry.options))}
            expanded = {ent.entity_id for device_id in devices for ent in choices.get(device_id, ())}
            device_sensors = {entity_id for entity_id in expanded if entity_id.startswith("sensor.")}
            if len(chosen) + len(binary) + len(people) + len(heat) > payload.MAX_ENTITIES:  # HA-012, HA-013: one cap of 64 for all four lists
                errors[CONF_ENTITIES] = errors[CONF_BINARY] = errors[CONF_PEOPLE] = errors[CONF_HEAT] = "too_many"
            elif not set(control) <= set(chosen) | set(heat):  # also keeps the control list within the 64 cap (HA-014: a heat entity may be ticked too)
                errors[CONF_CONTROL] = "control_not_shared"
            elif len(sensors) > payload.MAX_SENSORS:
                errors[CONF_SENSORS] = "too_many_sensors"
            elif not set(devices) <= choices.keys():  # HA-021: a device of an energy integration or the Energy dashboard, or one that is gone
                errors[CONF_DEVICES] = "device_not_shareable"
            elif (len(set(chosen + binary + people + heat) | (expanded - device_sensors)) > payload.MAX_DEVICE_ENTITIES
                  or len(set(sensors) | device_sensors) > payload.MAX_DEVICE_SENSORS):  # HA-021: what the devices expand to, against the raised caps
                errors[CONF_DEVICES] = "too_many_device_entities"
            else:
                self._data = {CONF_ENTITIES: chosen, CONF_CONTROL: control, CONF_SENSORS: sensors, CONF_BINARY: binary, CONF_PEOPLE: people, CONF_HEAT: heat,
                              CONF_DEVICES: devices}
                if solax_remote(self.hass) is None:  # BAT-008: no single hub with all four entities: the option is off
                    self._data[CONF_INVERTER] = False
                    return await self._wallbox_or_done()
                return await self.async_step_inverter()
        excluded = unshared_entries(self.hass)  # DEYE-003: a Deye entry's entities are never offered
        options = self._options(lambda ent: shareable(ent, excluded))
        sensor_options = self._options(lambda ent: sensor_shareable(ent, excluded))
        binary_options = self._options(lambda ent: binary_shareable(ent, excluded))
        people_options = self._options(person_shareable)
        heat_options = self._options(lambda ent: heat_shareable(ent, excluded))
        # PLAN-031 integration: a heat entity without a target is not offered for control (HA-021, D7: one with a low and a high target is)
        control_options = options + [o for o in heat_options if heat_controllable(self.hass.states.get(o["value"]))]
        device_options = [SelectOptionDict(value=device_id, label=label) for device_id, label, _ in device_choices(self.hass, other_wallbox_ids(self.config_entry.options))]
        offered = {CONF_ENTITIES: options, CONF_CONTROL: control_options, CONF_SENSORS: sensor_options, CONF_BINARY: binary_options, CONF_PEOPLE: people_options,
                   CONF_HEAT: heat_options, CONF_DEVICES: device_options}
        # A ticked device that is gone or no longer shareable would fail the selector's validation.
        current = {
            key: [e for e in (user_input or self.config_entry.options).get(key, []) if e in {o["value"] for o in listed}]
            for key, listed in offered.items()
        }
        selector = SelectSelector(SelectSelectorConfig(options=options, multiple=True, mode=SelectSelectorMode.LIST))
        sensor_selector = SelectSelector(SelectSelectorConfig(options=sensor_options, multiple=True, mode=SelectSelectorMode.LIST))
        binary_selector = SelectSelector(SelectSelectorConfig(options=binary_options, multiple=True, mode=SelectSelectorMode.LIST))
        people_selector = SelectSelector(SelectSelectorConfig(options=people_options, multiple=True, mode=SelectSelectorMode.LIST))
        control_selector = SelectSelector(SelectSelectorConfig(options=control_options, multiple=True, mode=SelectSelectorMode.LIST))  # HA-014
        heat_selector = SelectSelector(SelectSelectorConfig(options=heat_options, multiple=True, mode=SelectSelectorMode.LIST))
        # HA-021: whole devices first; a list of the devices that may be shared (an excluded one is never offered), as the entity lists are
        device_selector = SelectSelector(SelectSelectorConfig(options=device_options, multiple=True, mode=SelectSelectorMode.LIST))
        schema = vol.Schema({
            vol.Optional(CONF_DEVICES): device_selector,
            vol.Optional(CONF_ENTITIES): selector, vol.Optional(CONF_CONTROL): control_selector, vol.Optional(CONF_SENSORS): sensor_selector,
            vol.Optional(CONF_BINARY): binary_selector, vol.Optional(CONF_PEOPLE): people_selector,
            vol.Optional(CONF_HEAT): heat_selector,
        })
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, current), errors=errors
        )

    async def async_step_inverter(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """BAT-008: the separate inverter-control opt-in, off by default, with its own warning."""
        if user_input is not None:
            self._data[CONF_INVERTER] = bool(user_input.get(CONF_INVERTER))
            return await self._wallbox_or_done()
        schema = vol.Schema({vol.Required(CONF_INVERTER, default=bool(self.config_entry.options.get(CONF_INVERTER))): bool})
        return self.async_show_form(step_id="inverter", data_schema=schema)

    async def _wallbox_or_done(self) -> ConfigFlowResult:
        """EV-004: the Wallbox control step only with one EV-charger hub that has its three selects; else the option is off.
        WALLBOX-005: the Other wallbox step follows either way."""
        if solax_charger(self.hass) is None:
            self._data[CONF_WALLBOX] = False
            return await self.async_step_other_wallbox()
        return await self.async_step_wallbox()

    async def async_step_wallbox(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """EV-004: the separate wallbox-control opt-in, off by default, with its own warning."""
        if user_input is not None:
            self._data[CONF_WALLBOX] = bool(user_input.get(CONF_WALLBOX))
            return await self.async_step_other_wallbox()
        schema = vol.Schema({vol.Required(CONF_WALLBOX, default=bool(self.config_entry.options.get(CONF_WALLBOX))): bool})
        return self.async_show_form(step_id="wallbox", data_schema=schema)

    async def async_step_other_wallbox(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """WALLBOX-005: another charger: its power sensor and current number (A) are required together (both empty: none), a switch, a session
        energy sensor and a status sensor are optional, and the control opt-in is off by default with its own warning."""
        errors: dict[str, str] = {}
        if user_input is not None:
            values = {key: user_input.get(key) or None for key in OTHER_WALLBOX_FIELDS}
            number = self.hass.states.get(values[CONF_OW_CURRENT]) if values[CONF_OW_CURRENT] else None
            bad = payload.other_wallbox_error(values[CONF_OW_POWER], values[CONF_OW_CURRENT], number.attributes if number else None)
            if bad:
                errors[CONF_OW_POWER if bad[0] == "power" else CONF_OW_CURRENT] = bad[1]
            else:
                chosen = {key: value for key, value in values.items() if value and values[CONF_OW_POWER]}  # none without a wallbox
                return self.async_create_entry(data={**self._data, **chosen, CONF_OTHER_WALLBOX: bool(chosen and user_input.get(CONF_OTHER_WALLBOX))})
        schema = vol.Schema({
            **{vol.Optional(key): EntitySelector(config) for key, config in OTHER_WALLBOX_FIELDS.items()},
            vol.Required(CONF_OTHER_WALLBOX, default=bool(self.config_entry.options.get(CONF_OTHER_WALLBOX))): bool,
        })
        current = user_input or self.config_entry.options
        return self.async_show_form(
            step_id="other_wallbox", data_schema=self.add_suggested_values_to_schema(schema, {key: current[key] for key in OTHER_WALLBOX_FIELDS if current.get(key)}),
            errors=errors,
        )

    def _options(self, rule) -> list[SelectOptionDict]:
        """The registry entities the rule allows, labelled "name (entity id)" and sorted by label."""
        return sorted(
            (
                SelectOptionDict(value=ent.entity_id, label=f"{self._name(ent)} ({ent.entity_id})")
                for ent in er.async_get(self.hass).entities.values()
                if rule(ent)
            ),
            key=lambda option: option["label"].casefold(),
        )

    def _name(self, ent: er.RegistryEntry) -> str:
        state = self.hass.states.get(ent.entity_id)
        return state.name if state else ent.name or ent.original_name or ent.entity_id

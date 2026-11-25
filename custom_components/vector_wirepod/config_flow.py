"""Config flow. Validates against wire-pod's robot list, never the camera, so a
sleeping robot can be added."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (SelectOptionDict, SelectSelector,
                                            SelectSelectorConfig)

from .const import (BATTERY_POLL_S, CONF_BATTERY_POLL_S, CONF_FALLBACK_URL,
                    CONF_NAME, CONF_QUIET_HOLD_S, CONF_SERIAL,
                    CONF_STILL_REFRESH_S, CONF_WIREPOD_URL, DEFAULT_QUIET_HOLD_S,
                    DEFAULT_STILL_REFRESH_S, DEFAULT_WIREPOD_URL, DOMAIN,
                    MAX_BATTERY_POLL_S, MAX_QUIET_HOLD_S, MAX_STILL_REFRESH_S,
                    MIN_BATTERY_POLL_S, MIN_QUIET_HOLD_S)
from .failover import normalize_pod_url
from .wirepod import WirePodError, get_sdk_info


class VectorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url = ""
        self._fallback_url = ""
        self._robots: list[dict] = []

    async def _probe_one(self, url: str) -> tuple[list[dict] | None, str | None]:
        assert self.hass is not None and isinstance(url, str)
        try:
            robots = await get_sdk_info(async_get_clientsession(self.hass), url)
        except ValueError:
            return None, "invalid_url"
        except (aiohttp.ClientError, TimeoutError, WirePodError):
            return None, "cannot_connect"
        robots = [r for r in robots if r["esn"].isalnum()]
        if not robots:
            return None, "no_robots"
        return robots, None

    async def _probe_robots(self, url: str, fallback_url: str = "") -> tuple[list[dict] | None, str | None]:
        robots, err = await self._probe_one(url)
        if err == "cannot_connect" and fallback_url and fallback_url != url:
            fb_robots, fb_err = await self._probe_one(fallback_url)
            if fb_err is None:
                return fb_robots, None
        return robots, err

    def _parse_urls(self, user_input: dict[str, Any]) -> tuple[str, str]:
        assert isinstance(user_input, dict)
        url = normalize_pod_url(user_input.get(CONF_WIREPOD_URL, ""))
        fb = normalize_pod_url(user_input.get(CONF_FALLBACK_URL, ""), allow_empty=True)
        return url, ("" if fb == url else fb)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        cur_url, cur_fb = DEFAULT_WIREPOD_URL, ""
        if user_input is not None:
            try:
                cur_url, cur_fb = self._parse_urls(user_input)
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                robots, err = await self._probe_robots(cur_url, cur_fb)
                if err is None and robots is not None:
                    self._url, self._fallback_url, self._robots = cur_url, cur_fb, robots
                    return await self.async_step_robot()
                if err is not None:
                    errors["base"] = err
        schema = vol.Schema({
            vol.Required(CONF_WIREPOD_URL, default=cur_url): str,
            vol.Optional(CONF_FALLBACK_URL, default=cur_fb): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_robot(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._url and self._robots
        if user_input is not None:
            serial = user_input[CONF_SERIAL]
            await self.async_set_unique_id(serial)
            self._abort_if_unique_id_configured()
            title = (user_input.get(CONF_NAME) or "").strip() or f"Vector {serial}"
            data = {CONF_WIREPOD_URL: self._url, CONF_SERIAL: serial}
            if self._fallback_url:
                data[CONF_FALLBACK_URL] = self._fallback_url
            return self.async_create_entry(title=title, data=data)
        options = [SelectOptionDict(value=r["esn"], label=f"{r['esn']} ({r['ip_address']})")
                   for r in self._robots]
        schema = vol.Schema({
            vol.Required(CONF_SERIAL): SelectSelector(SelectSelectorConfig(options=options)),
            vol.Optional(CONF_NAME, default=""): str,
        })
        return self.async_show_form(step_id="robot", data_schema=schema)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        cur_url = entry.data[CONF_WIREPOD_URL]
        cur_fb = entry.options.get(CONF_FALLBACK_URL) or entry.data.get(CONF_FALLBACK_URL, "")
        if user_input is not None:
            try:
                cur_url, cur_fb = self._parse_urls(user_input)
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                robots, err = await self._probe_robots(cur_url, cur_fb)
                if err is not None:
                    errors["base"] = err
                elif robots is not None and not any(r["esn"] == entry.data[CONF_SERIAL] for r in robots):
                    errors["base"] = "robot_not_on_server"
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_WIREPOD_URL: cur_url, CONF_FALLBACK_URL: cur_fb})
        schema = vol.Schema({
            vol.Required(CONF_WIREPOD_URL, default=cur_url): str,
            vol.Optional(CONF_FALLBACK_URL, default=cur_fb): str,
        })
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VectorOptionsFlow()


class VectorOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self.config_entry is not None
        errors: dict[str, str] = {}
        cur_fb = self.config_entry.options.get(
            CONF_FALLBACK_URL, self.config_entry.data.get(CONF_FALLBACK_URL, ""))
        if user_input is not None:
            try:
                fb = normalize_pod_url(user_input.get(CONF_FALLBACK_URL, ""), allow_empty=True)
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                out = dict(user_input)
                out[CONF_FALLBACK_URL] = "" if fb == self.config_entry.data.get(CONF_WIREPOD_URL) else fb
                return self.async_create_entry(data=out)
        cur_refresh = self.config_entry.options.get(CONF_STILL_REFRESH_S, DEFAULT_STILL_REFRESH_S)
        cur_poll = self.config_entry.options.get(CONF_BATTERY_POLL_S, BATTERY_POLL_S)
        cur_hold = self.config_entry.options.get(CONF_QUIET_HOLD_S, DEFAULT_QUIET_HOLD_S)
        schema = vol.Schema({
            vol.Required(CONF_STILL_REFRESH_S, default=cur_refresh):
                vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_STILL_REFRESH_S)),
            vol.Required(CONF_BATTERY_POLL_S, default=cur_poll):
                vol.All(vol.Coerce(int), vol.Range(min=MIN_BATTERY_POLL_S, max=MAX_BATTERY_POLL_S)),
            vol.Required(CONF_QUIET_HOLD_S, default=cur_hold):
                vol.All(vol.Coerce(int), vol.Range(min=MIN_QUIET_HOLD_S, max=MAX_QUIET_HOLD_S)),
            vol.Optional(CONF_FALLBACK_URL, default=cur_fb): str,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

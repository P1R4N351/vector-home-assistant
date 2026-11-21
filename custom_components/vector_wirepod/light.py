"""Light entity: the backlight of Vector's face screen.

The pod reads and writes it over the robot's own login, which only developer and OSKR robots
have. On any other robot, or a pod without that call, the entity stays unavailable.
The robot also changes the level by itself (measured 2026-11-19: 10 awake, 5 asleep),
so the level is read again every SCAN_INTERVAL and after every change made here.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .camera import device_info
from .const import CONF_SERIAL
from .cryosleep import bind_cryosleep_entity, cryosleep_displaced, guard_not_cryosleep
from .wirepod import BACKLIGHT_MAX, WirePodError, get_backlight, set_backlight

SCAN_INTERVAL = timedelta(seconds=300)
PARALLEL_UPDATES = 1
DEFAULT_LEVEL = 10
FAILURES = (WirePodError, aiohttp.ClientError, TimeoutError, ValueError)


def level_to_brightness(level: int) -> int:
    return round(max(0, min(BACKLIGHT_MAX, level)) * 255 / BACKLIGHT_MAX)


def brightness_to_level(brightness: int) -> int:
    """A brightness above zero never rounds down to off."""
    return max(1, min(BACKLIGHT_MAX, round(brightness * BACKLIGHT_MAX / 255)))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([FaceBacklight(entry)], update_before_add=True)


class FaceBacklight(LightEntity):
    _attr_has_entity_name = True
    _attr_name = "Face backlight"
    _attr_icon = "mdi:robot-outline"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._data = entry.runtime_data
        self._level: int | None = None
        self._last_on_level = DEFAULT_LEVEL
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_face_backlight"
        self._attr_device_info = device_info(entry)

    def _took(self, level: int | None) -> None:
        self._level = level
        if level:
            self._last_on_level = level

    @property
    def available(self) -> bool:
        return not cryosleep_displaced(getattr(self._data, "manager", None)) and self._level is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        bind_cryosleep_entity(self, getattr(self._data, "manager", None), refresh_on_exit=True)

    @property
    def is_on(self) -> bool | None:
        return None if self._level is None else self._level > 0

    @property
    def brightness(self) -> int | None:
        return None if self._level is None else level_to_brightness(self._level)

    @property
    def extra_state_attributes(self) -> dict:
        return {"level": self._level, "max_level": BACKLIGHT_MAX}

    async def async_update(self) -> None:
        data = self._data
        mgr = getattr(data, "manager", None)
        if cryosleep_displaced(mgr):
            if self._level is not None:
                return
            saved = getattr(getattr(mgr, "cryosleep", None), "saved_backlight", None)
            if saved is not None:
                self._took(saved)
                return
        try:
            self._took(await get_backlight(data.session, data.base_url, data.serial))
        except FAILURES:
            self._level = None

    async def _set(self, level: int) -> None:
        data = self._data
        guard_not_cryosleep(getattr(data, "manager", None), HomeAssistantError)
        note = getattr(getattr(data, "manager", None), "note_manual_backlight", None)
        if callable(note):
            note()
        try:
            self._took(await set_backlight(data.session, data.base_url, data.serial, level))
        except FAILURES as exc:
            raise HomeAssistantError(f"The backlight was not changed: {exc}") from exc
        if self._level is None:
            raise HomeAssistantError("This robot or pod offers no backlight control")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            await self._set(brightness_to_level(int(kwargs[ATTR_BRIGHTNESS])))
        else:
            await self._set(self._last_on_level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(0)

"""Number entity: master speaker volume slider for Vector. 

Vector stores master_volume as integer 0..5 (mute through high) in its ROBOT_SETTINGS jdoc.
Reading /api-sdk/get_sdk_settings and writing /api-sdk/volume use Vector's standard SDK
connection (unlike backlight or mic mute, no SSH login to the robot is needed). Because quiet
mode mutes master_volume while active and saves any earlier non-zero volume to restore on
release, this entity polls every SCAN_INTERVAL as well as updating immediately on change.
"""

from __future__ import annotations

from datetime import timedelta

import aiohttp

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .camera import device_info
from .const import CONF_SERIAL, POLL_TIMEOUT_S, SIGNAL_RECOVERED
from .cryosleep import bind_cryosleep_entity, cryosleep_displaced, guard_not_cryosleep
from .manual_numbers import manual_numbers
from .wirepod import (
    VOLUME_MAX,
    VOLUME_MIN,
    VOLUME_NAMES,
    WirePodError,
    get_volume,
    set_volume,
)

SCAN_INTERVAL = timedelta(seconds=300)
PARALLEL_UPDATES = 1
FAILURES = (WirePodError, aiohttp.ClientError, TimeoutError, ValueError)


def volume_icon(level: int | None) -> str:
    """Return a Material Design speaker icon matching volume level 0..5."""
    assert level is None or (not isinstance(level, bool) and isinstance(level, int))
    if level is not None and not VOLUME_MIN <= level <= VOLUME_MAX:
        raise ValueError(f"invalid volume level: {level!r}")
    if level == 0:
        return "mdi:volume-mute"
    if level in (1, 2):
        return "mdi:volume-low"
    if level == VOLUME_MAX:
        return "mdi:volume-high"
    return "mdi:volume-medium"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert hass is not None and entry is not None
    assert callable(async_add_entities)
    async_add_entities([VectorVolumeNumber(entry)], update_before_add=True)
    async_add_entities(manual_numbers(hass, entry))


class VectorVolumeNumber(NumberEntity):
    """Master volume slider (0 = mute .. 5 = high) for one Vector."""

    _attr_has_entity_name = True
    _attr_name = "Volume"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = float(VOLUME_MIN)
    _attr_native_max_value = float(VOLUME_MAX)
    _attr_native_step = 1.0
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        assert entry is not None and entry.runtime_data is not None
        assert isinstance(entry.data.get(CONF_SERIAL), str)
        self._data = entry.runtime_data
        self._level: int | None = None
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_volume"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        return not cryosleep_displaced(getattr(self._data, "manager", None)) and self._level is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        bind_cryosleep_entity(self, getattr(self._data, "manager", None), refresh_on_exit=True)
        if getattr(self, "hass", None) is not None:
            @callback
            def _on_recovered() -> None:
                self.async_schedule_update_ha_state(True)

            self.async_on_remove(async_dispatcher_connect(
                self.hass, SIGNAL_RECOVERED.format(self._data.serial), _on_recovered))

    @property
    def native_value(self) -> float | None:
        return None if self._level is None else float(self._level)

    @property
    def icon(self) -> str:
        return volume_icon(self._level)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        name = None if self._level is None else VOLUME_NAMES[self._level]
        return {"level_name": name, "levels": list(VOLUME_NAMES)}

    async def async_update(self) -> None:
        """Read master volume from wire-pod unless the coordinator reports unreachable."""
        data = self._data
        assert data is not None and data.session is not None
        mgr = getattr(data, "manager", None)
        if cryosleep_displaced(mgr) and (self._level is not None or bool(getattr(getattr(mgr, "cryosleep", None), "holding_reserve", False))):
            return
        coord_data = data.coordinator.data if data.coordinator is not None else None
        if isinstance(coord_data, dict) and coord_data.get("robot_reachable") is False:
            self._level = None
            return
        try:
            vol = await get_volume(data.session, data.base_url, data.serial, timeout_s=POLL_TIMEOUT_S)
        except FAILURES:
            self._level = None
            return
        assert vol is None or (not isinstance(vol, bool) and isinstance(vol, int))
        self._level = vol

    async def async_set_native_value(self, value: float) -> None:
        """Set Vector's master volume (0..5) through wire-pod."""
        data = self._data
        guard_not_cryosleep(getattr(data, "manager", None), HomeAssistantError)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HomeAssistantError(f"Invalid volume value: {value!r}")
        target = round(float(value))
        if not VOLUME_MIN <= target <= VOLUME_MAX:
            raise HomeAssistantError(f"Volume must be {VOLUME_MIN} to {VOLUME_MAX}: {target!r}")
        try:
            await set_volume(data.session, data.base_url, data.serial, target)
        except FAILURES as exc:
            raise HomeAssistantError(f"The volume was not changed: {exc}") from exc
        self._level = target
        self.async_write_ha_state()

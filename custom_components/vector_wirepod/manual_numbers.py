"""Number entities for Vector's head angle and lift height."""

from __future__ import annotations

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import controls
from .camera import device_info
from .const import CONF_SERIAL
from .manual import ManualControl, ensure_control


def _parse_restored_float(raw: str | None, low: float, high: float) -> float | None:
    """Return finite float within [low, high] from a restored state string, or None."""
    if not isinstance(raw, str):
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if not math.isfinite(val) or not low <= val <= high:
        return None
    return val


def manual_numbers(hass: HomeAssistant, entry: ConfigEntry) -> list[NumberEntity]:
    assert hass is not None and entry is not None
    control = ensure_control(hass, entry)
    return [HeadAngleNumber(entry, control), LiftHeightNumber(entry, control)]


class _ManualNumber(NumberEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_assumed_state = True
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: ConfigEntry, control: ManualControl, key: str, name: str) -> None:
        assert entry is not None and control is not None and bool(key)
        self._control = control
        self._attr_name = name
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_manual_{key}"
        self._attr_device_info = device_info(entry)

    def _apply_restored(self, value: float) -> None:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.native_value is None:
            last = await self.async_get_last_state()
            raw = None if last is None else getattr(last, "state", None)
            val = _parse_restored_float(raw, float(self._attr_native_min_value),
                                        float(self._attr_native_max_value))
            if val is not None:
                self._apply_restored(val)
        self.async_on_remove(self._control.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return not self._control.displaced()


class HeadAngleNumber(_ManualNumber):
    _attr_icon = "mdi:head-cog-outline"
    _attr_native_min_value = controls.HEAD_MIN_DEG
    _attr_native_max_value = controls.HEAD_MAX_DEG
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "°"

    def __init__(self, entry: ConfigEntry, control: ManualControl) -> None:
        super().__init__(entry, control, "head_angle", "Head angle")

    def _apply_restored(self, value: float) -> None:
        self._control.head_deg = value

    @property
    def native_value(self) -> float | None:
        return self._control.head_deg

    async def async_set_native_value(self, value: float) -> None:
        answer = await self._control.command(controls.PATH_HEAD, {"angle_deg": float(value)})
        got = answer.get("angle_deg")
        self._control.head_deg = float(got) if isinstance(got, (int, float)) else float(value)
        self.async_write_ha_state()


class LiftHeightNumber(_ManualNumber):
    _attr_icon = "mdi:forklift"
    _attr_native_min_value = controls.LIFT_MIN_PCT
    _attr_native_max_value = controls.LIFT_MAX_PCT
    _attr_native_step = 5.0
    _attr_native_unit_of_measurement = "%"

    def __init__(self, entry: ConfigEntry, control: ManualControl) -> None:
        super().__init__(entry, control, "lift_height", "Lift height")

    def _apply_restored(self, value: float) -> None:
        self._control.lift_pct = value

    @property
    def native_value(self) -> float | None:
        return self._control.lift_pct

    async def async_set_native_value(self, value: float) -> None:
        answer = await self._control.command(controls.PATH_LIFT, {"height_pct": float(value)})
        got = answer.get("height_pct")
        self._control.lift_pct = float(got) if isinstance(got, (int, float)) else float(value)
        self.async_write_ha_state()

"""'Manual control' switch for one Vector.

On while Home Assistant holds behavior control (taken by any manual control, or by this switch).
Turning it off lets go at once; otherwise control is let go a minute after the last command.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry

from .camera import device_info
from .const import CONF_SERIAL
from .manual import ManualControl, ensure_control


def manual_switches(hass, entry: ConfigEntry) -> list[SwitchEntity]:
    control = ensure_control(hass, entry)
    return [ManualControlSwitch(entry, control)]


class ManualControlSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Manual control"
    _attr_icon = "mdi:controller"

    def __init__(self, entry: ConfigEntry, control: ManualControl) -> None:
        assert entry is not None and control is not None
        self._control = control
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_manual_control"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._control.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return not self._control.displaced()

    @property
    def is_on(self) -> bool:
        return self._control.hold.held

    @property
    def extra_state_attributes(self) -> dict:
        return {"releases_in_s": self._control.seconds_until_release()}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._control.command(None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._control.release()

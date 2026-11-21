"""Drive, turn and stop buttons for one Vector.

Each press is one short step through controls.py at DEFAULT priority, so Vector's own cliff,
fall and pick-up reactions still interrupt it. Stop works at any time the robot is reachable.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry

from . import controls
from .camera import device_info
from .const import CONF_SERIAL
from .manual import ManualControl, ensure_control

STEP_BUTTONS = (
    ("forward", "Drive forward", "mdi:arrow-up-bold"),
    ("backward", "Drive backward", "mdi:arrow-down-bold"),
    ("turn_left", "Turn left", "mdi:rotate-left"),
    ("turn_right", "Turn right", "mdi:rotate-right"),
)


def manual_buttons(hass, entry: ConfigEntry) -> list[ButtonEntity]:
    control = ensure_control(hass, entry)
    return [*[StepButton(entry, control, *spec) for spec in STEP_BUTTONS], StopButton(entry, control)]


def step_params(key: str) -> tuple[str, dict]:
    """The path and arguments of one step button."""
    path, amount, speed = controls.STEPS[key]
    if path == controls.PATH_DRIVE:
        return path, {"dist_mm": amount, "speed_mmps": speed}
    return path, {"angle_deg": amount, "speed_dps": speed}


class _ManualButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, control: ManualControl, key: str, name: str, icon: str) -> None:
        assert entry is not None and control is not None and bool(key)
        self._control = control
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_manual_{key}"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._control.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return not self._control.displaced()


class StepButton(_ManualButton):
    async def async_press(self) -> None:
        path, params = step_params(self._key)
        await self._control.command(path, params)


class StopButton(_ManualButton):
    def __init__(self, entry: ConfigEntry, control: ManualControl) -> None:
        super().__init__(entry, control, "stop", "Stop", "mdi:stop-circle")

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self._control.stop()

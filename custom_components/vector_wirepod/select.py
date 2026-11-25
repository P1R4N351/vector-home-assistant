"""'Eye colour' select for one Vector. 

The seven presets are the SDK's EyeColor values. 'Custom' is shown when the robot has a custom
colour set (in the wire-pod web page or the Vector app) and choosing it puts that colour back.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import controls
from .camera import device_info
from .const import CONF_SERIAL
from .manual import ManualControl, ensure_control, ensure_extras
from .robotdata import EYE_CUSTOM, EYE_PRESETS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert hass is not None and entry is not None and callable(async_add_entities)
    control, extras = ensure_control(hass, entry), ensure_extras(hass, entry)
    async_add_entities([EyeColourSelect(entry, control, extras)])


def eye_command(option: str, settings: dict | None) -> tuple[str, dict]:
    """The control that sets this option. Raises ValueError when it cannot be set."""
    if option in EYE_PRESETS:
        return controls.PATH_EYE, {"color": EYE_PRESETS.index(option)}
    if option != EYE_CUSTOM:
        raise ValueError(f"unknown eye colour {option!r}")
    hue = (settings or {}).get("custom_hue")
    sat = (settings or {}).get("custom_saturation")
    if not isinstance(hue, float) or not isinstance(sat, float):
        raise ValueError("no custom colour is known; set one in the wire-pod web page first")
    return controls.PATH_EYE_CUSTOM, {"hue": hue, "sat": sat}


class EyeColourSelect(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = "Eye colour"
    _attr_icon = "mdi:eye"
    _attr_options = [*EYE_PRESETS, EYE_CUSTOM]

    def __init__(self, entry: ConfigEntry, control: ManualControl, extras) -> None:
        assert entry is not None and control is not None and extras is not None
        super().__init__(extras)
        self._control = control
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_eye_colour"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._control.add_listener(self.async_write_ha_state))

    def _settings(self) -> dict | None:
        data = self.coordinator.data
        settings = data.get("settings") if isinstance(data, dict) else None
        return settings if isinstance(settings, dict) else None

    @property
    def available(self) -> bool:
        return self._settings() is not None and not self._control.displaced()

    @property
    def current_option(self) -> str | None:
        settings = self._settings()
        return None if settings is None else settings.get("eye_color")

    @property
    def extra_state_attributes(self) -> dict:
        settings = self._settings() or {}
        return {"custom_hue": settings.get("custom_hue"), "custom_saturation": settings.get("custom_saturation")}

    async def async_select_option(self, option: str) -> None:
        try:
            path, params = eye_command(option, self._settings())
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self._control.setting(path, params)
        settings = self._settings()
        if settings is not None:
            settings["eye_color"] = option
        self.async_write_ha_state()

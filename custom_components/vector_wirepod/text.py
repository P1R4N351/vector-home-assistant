"""'Say' text box: Vector says what is typed into it."""

from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import controls
from .camera import device_info
from .const import CONF_SERIAL
from .manual import ManualControl, ensure_control


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert hass is not None and entry is not None and callable(async_add_entities)
    control = ensure_control(hass, entry)
    async_add_entities([SayText(entry, control)])


class SayText(TextEntity):
    _attr_has_entity_name = True
    _attr_name = "Say"
    _attr_icon = "mdi:account-voice"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = controls.SAY_MAX_CHARS

    def __init__(self, entry: ConfigEntry, control: ManualControl) -> None:
        assert entry is not None and control is not None
        self._control = control
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_say"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._control.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return not self._control.displaced()

    @property
    def native_value(self) -> str:
        return ""

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"last_said": self._control.last_said}

    async def async_set_value(self, value: str) -> None:
        if isinstance(value, str) and not value.strip():
            self.async_write_ha_state()
            return
        try:
            text = controls.check_say_text(value)
        except ValueError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self._control.say(text)
        self._control.last_said = text
        self.async_write_ha_state()

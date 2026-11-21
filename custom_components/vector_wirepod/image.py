"""Image entity: the newest stored photo from one Vector."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import photos
from .camera import device_info
from .const import CONF_SERIAL, DOMAIN, SIGNAL_PHOTO


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VectorLastPhoto(hass, entry)])


class VectorLastPhoto(ImageEntity):
    _attr_has_entity_name = True
    _attr_name = "Last photo"
    _attr_icon = "mdi:image-outline"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass)
        self._manager = entry.runtime_data.manager
        self._path = None
        self._count = 0
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_last_photo"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._reload()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_PHOTO.format(self._manager.serial), self._on_photo))

    async def _reload(self) -> None:
        stored = await self.hass.async_add_executor_job(photos.list_photos, self._manager.photo_dir)
        self._count = len(stored)
        self._path = stored[-1] if stored else None
        self._attr_image_last_updated = photos.taken_at(self._path) if self._path else None

    @callback
    def _on_photo(self) -> None:
        self.hass.async_create_task(self._reload_and_write())

    async def _reload_and_write(self) -> None:
        await self._reload()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict:
        path = self._path
        serial = self._manager.serial.lower()
        return {
            "file": path.name if path else None,
            "reason": photos.reason_of(path) if path else None,
            "url": f"/local/{DOMAIN}/{serial}/{path.name}" if path else None,
            "stored_photos": self._count,
            "last_error": self._manager.last_photo_error,
        }

    async def async_image(self) -> bytes | None:
        if self._path is None:
            return None
        try:
            return await self.hass.async_add_executor_job(self._path.read_bytes)
        except OSError:
            return None

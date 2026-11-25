"""Camera entity for one Vector, passive and shared. 

Frames are passed on exactly as the robot sent them, whatever their shape: Vector 1.0 sends 16:9
and Vector 2.0 sends 4:3. Status images take the same shape as the robot's frames.

Thumbnails never open the robot's camera on their own schedule: they are served
from the last frame, refreshed at most once per still_refresh_s and only when a
start is due (see CameraHub.still). Opening the live view takes a lease on the
one shared upstream; closing it releases the lease.
"""

from __future__ import annotations

from aiohttp import web

from homeassistant.components.camera import Camera, async_get_still_stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import hardware, placeholder
from .const import (BATTERY_LEVELS, CONF_SERIAL, CONF_STILL_REFRESH_S, CONF_WIREPOD_URL,
                    DEFAULT_STILL_REFRESH_S, DOMAIN, MAX_STREAM_STATUS_FRAMES,
                    STATUS_KEEPALIVE_S, STILL_WAIT_S)
from .wirepod import STATE_STARTING, STATE_STREAMING

LIVE_INTERVAL_S = 0.05
FRESH_WHEN_NO_REFRESH_S = 60.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([VectorCamera(entry)])


def device_info(entry: ConfigEntry) -> DeviceInfo:
    serial = entry.data[CONF_SERIAL]
    data = getattr(entry, "runtime_data", None)
    size = data.hub.frame_size() if data is not None else None
    maker, model = hardware.maker_and_model(serial, size)
    return DeviceInfo(identifiers={(DOMAIN, serial)}, name=entry.title,
                      manufacturer=maker, model=model, serial_number=serial,
                      configuration_url=entry.data.get(CONF_WIREPOD_URL))


def _fmt_age(age: float) -> str:
    if age < 120:
        return f"{int(age)} s"
    if age < 7200:
        return f"{int(age // 60)} min"
    return f"{age / 3600:.1f} h"


class VectorCamera(CoordinatorEntity, Camera, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: ConfigEntry) -> None:
        data = entry.runtime_data
        CoordinatorEntity.__init__(self, data.coordinator)
        Camera.__init__(self)
        self._entry = entry
        self._hub = data.hub
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_camera"
        self._attr_device_info = device_info(entry)
        self._refresh_s = int(entry.options.get(CONF_STILL_REFRESH_S, DEFAULT_STILL_REFRESH_S))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._hub.add_listener(self.async_write_ha_state))
        # The camera's shape (16:9 on Vector 1.0, 4:3 on 2.0) is learnt from frames; keep it
        # over a restart so status images have the right shape before the first frame.
        last = await self.async_get_last_state()
        if last is not None:
            width, height = last.attributes.get("frame_width"), last.attributes.get("frame_height")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                self._hub.seed_frame_size((width, height))

    @property
    def is_streaming(self) -> bool:
        return self._hub.state == STATE_STREAMING

    @property
    def extra_state_attributes(self) -> dict:
        assert self._hub is not None and self.coordinator is not None
        hub = self._hub
        age = hub.frame_age_s()
        data = self.coordinator.data or {}
        size = hub.frame_size()
        return {
            "frame_width": size[0] if size else None,
            "frame_height": size[1] if size else None,
            "aspect_ratio": round(size[0] / size[1], 3) if size else None,
            "camera_state": hub.state,
            "camera_detail": hub.detail,
            "camera_allowed": hub.allowed,
            "viewers": hub.viewers,
            "last_frame_age_s": None if age is None else round(age, 1),
            "next_start_in_s": round(hub.retry_in_s(), 1),
            "dropped_frames": hub.dropped_frames,
            "active_pod_url": getattr(self._entry.runtime_data.session, "active_url", self._entry.runtime_data.base_url),
            "robot_reachable": data.get("robot_reachable"),
            "on_charger": data.get("is_on_charger_platform"),
            "charging": data.get("is_charging"),
            "battery_level": BATTERY_LEVELS.get(data.get("battery_level"), "unknown"),
            "battery_volts": data.get("battery_volts"),
        }

    def _fresh_s(self) -> float:
        return float(self._refresh_s) if self._refresh_s > 0 else FRESH_WHEN_NO_REFRESH_S

    def _status_lines(self) -> list[str]:
        assert self._hub is not None and self._entry is not None
        hub = self._hub
        data = self.coordinator.data or {}
        age = hub.frame_age_s()
        lines = [self._entry.title, f"Not live: camera {hub.state.replace('_', ' ')}"]
        if hub.detail:
            lines.append(hub.detail)
        lines.append("No frame yet" if age is None else f"Last frame {_fmt_age(age)} ago")
        if data.get("robot_reachable"):
            if data.get("is_on_charger_platform"):
                place = "on charger (charging)" if data.get("is_charging") else "on charger"
            else:
                place = "off charger"
            level = BATTERY_LEVELS.get(data.get("battery_level"), "unknown")
            lines.append(f"Battery {level}, {place}")
        return lines

    async def _status_image(self, frame: bytes | None) -> bytes:
        return await self.hass.async_add_executor_job(
            placeholder.render, self._status_lines(), frame, self._hub.frame_size())

    async def async_camera_image(self, width: int | None = None,
                                 height: int | None = None) -> bytes | None:
        hub = self._hub
        fresh = self._fresh_s()
        frame = await hub.still(fresh, self._refresh_s > 0, STILL_WAIT_S)
        age = hub.frame_age_s()
        if frame is not None and age is not None and age <= fresh:
            return frame
        return await self._status_image(frame)

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse | None:
        assert request is not None and self._hub is not None
        hub = self._hub
        lease = hub.acquire()
        state = {"last": hub.seq, "status_sent": False, "status_frames": 0}
        wait_s = hub.cfg.header_timeout_s + hub.cfg.stall_timeout_s

        async def next_image() -> bytes | None:
            return await self._next_stream_image(state, wait_s)

        try:
            return await async_get_still_stream(request, next_image, "image/jpeg",
                                                LIVE_INTERVAL_S)
        finally:
            lease.release()

    async def _next_stream_image(self, state: dict, wait_s: float) -> bytes | None:
        assert wait_s > 0 and isinstance(state, dict)
        hub = self._hub
        got = None
        if not state["status_sent"] or hub.can_start() or hub.state in (STATE_STARTING, STATE_STREAMING):
            got = await hub.next_frame(state["last"], wait_s)
        if got is not None:
            state["last"], state["status_sent"], state["status_frames"] = got[0], False, 0
            return got[1]
        if not state["status_sent"]:
            state["status_sent"] = True
            state["status_frames"] = 1
            return await self._status_image(hub.frame)
        if state["status_frames"] >= MAX_STREAM_STATUS_FRAMES:
            return None
        got = await hub.wait_wake(state["last"], STATUS_KEEPALIVE_S)
        if got is not None:
            state["last"], state["status_sent"], state["status_frames"] = got[0], False, 0
            return got[1]
        state["status_frames"] += 1
        return await self._status_image(hub.frame)

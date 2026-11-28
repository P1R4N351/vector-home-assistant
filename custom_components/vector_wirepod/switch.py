"""Switch entities: 'Camera allowed', 'Quiet mode', 'Photo on wake' and 'Mute microphones' for Vector.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera import device_info
from .const import CONF_SERIAL, DOMAIN, POLL_TIMEOUT_S, SIGNAL_RECOVERED
from .cryosleep import bind_cryosleep_entity, cryosleep_displaced, guard_not_cryosleep
from .manual_switch import manual_switches
from .wirepod import WirePodError, get_mic_mute, set_mic_mute, set_quiet_mode

# Only 'Mute microphones' polls; the robot can also change it itself (a back-button double press
# off the charger), so it is read again this often.
SCAN_INTERVAL = timedelta(seconds=300)
MIC_FAILURES = (WirePodError, aiohttp.ClientError, TimeoutError, ValueError)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert hass is not None and entry is not None
    async_add_entities([
        CryosleepSwitch(entry),
        CameraAllowedSwitch(entry),
        QuietModeSwitch(entry),
        PhotoOnWakeSwitch(entry),
    ])
    async_add_entities([MicMuteSwitch(entry)], update_before_add=True)
    async_add_entities(manual_switches(hass, entry))


class CryosleepSwitch(SwitchEntity, RestoreEntity):
    """Overnight Cryosleep mode: mute mic/speakers, enforce sleep, darken screen, displace controls."""

    _attr_has_entity_name = True
    _attr_name = "Cryosleep"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        assert entry is not None and entry.runtime_data is not None
        self._manager = entry.runtime_data.manager
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_cryosleep"
        self._attr_device_info = device_info(entry)

    @property
    def is_on(self) -> bool:
        return self._manager.cryosleep.enabled

    @property
    def icon(self) -> str:
        return "mdi:snowflake" if self.is_on else "mdi:snowflake-off"

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return self._manager.cryosleep.as_attributes()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._manager.restore_cryosleep(dict(last.attributes))
        bind_cryosleep_entity(self, self._manager)

    async def async_turn_on(self, **kwargs: Any) -> None:
        assert self._manager is not None
        self._manager.start_cryosleep()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        assert self._manager is not None
        self._manager.stop_cryosleep()
        self.async_write_ha_state()


class CameraAllowedSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = "Camera allowed"
    _attr_icon = "mdi:cctv"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._hub = entry.runtime_data.hub
        self._manager = entry.runtime_data.manager
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_camera_allowed"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        return not cryosleep_displaced(self._manager)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if not cryosleep_displaced(self._manager):
            self._hub.set_allowed(last is None or last.state != "off")
        bind_cryosleep_entity(self, self._manager)

    @property
    def is_on(self) -> bool:
        return self._hub.allowed

    async def async_turn_on(self, **kwargs: Any) -> None:
        guard_not_cryosleep(self._manager, HomeAssistantError)
        self._hub.set_allowed(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        guard_not_cryosleep(self._manager, HomeAssistantError)
        self._hub.set_allowed(False)
        self.async_write_ha_state()


class QuietModeSwitch(CoordinatorEntity, SwitchEntity, RestoreEntity):
    """Hold RESERVE_CONTROL + mute on Vector to suppress autonomous activity and expressions."""

    _attr_has_entity_name = True
    _attr_name = "Quiet mode"

    def __init__(self, entry: ConfigEntry) -> None:
        data = entry.runtime_data
        super().__init__(data.coordinator)
        self._data = data
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_quiet_mode"
        self._attr_device_info = device_info(entry)
        self._desired: bool = False
        self._pending_reconcile: bool | None = None

    @property
    def icon(self) -> str:
        return "mdi:sleep" if self.is_on else "mdi:sleep-off"

    @property
    def available(self) -> bool:
        if cryosleep_displaced(self._data.manager) or not super().available:
            return False
        qm = (self.coordinator.data or {}).get("quiet_mode")
        return isinstance(qm, dict)

    async def _reconcile_desired(self, want: bool, qm: dict) -> None:
        if not cryosleep_displaced(self._data.manager) and qm.get("enabled") != want:
            try:
                res = await set_quiet_mode(
                    self._data.session, self._data.base_url, self._data.serial, want)
                if isinstance(self.coordinator.data, dict):
                    self.coordinator.data["quiet_mode"] = res
            except (aiohttp.ClientError, TimeoutError, WirePodError):
                return
        self._pending_reconcile = None

    @callback
    def _on_recovered_quiet(self) -> None:
        qm = (self.coordinator.data or {}).get("quiet_mode")
        if self._pending_reconcile is not None and isinstance(qm, dict) and self.hass is not None:
            want = self._pending_reconcile
            self.hass.async_create_background_task(
                self._reconcile_desired(want, qm), f"{DOMAIN} quiet recover {self._data.serial}")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        bind_cryosleep_entity(self, self._data.manager)
        if getattr(self, "hass", None) is not None:
            self.async_on_remove(async_dispatcher_connect(
                self.hass, SIGNAL_RECOVERED.format(self._data.serial), self._on_recovered_quiet))
        last = await self.async_get_last_state()
        qm = (self.coordinator.data or {}).get("quiet_mode")
        want: bool | None = None
        if last is not None and last.state in ("on", "off"):
            want = last.state == "on"
        elif last is not None and isinstance(last.attributes.get("desired"), bool):
            want = last.attributes["desired"]
        if want is not None:
            self._desired = want
            if isinstance(qm, dict):
                await self._reconcile_desired(want, qm)
            else:
                self._pending_reconcile = want
        elif isinstance(qm, dict):
            self._desired = bool(qm.get("enabled"))

    @property
    def is_on(self) -> bool:
        qm = (self.coordinator.data or {}).get("quiet_mode")
        if isinstance(qm, dict):
            return bool(qm.get("enabled"))
        return self._desired

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        qm = (self.coordinator.data or {}).get("quiet_mode")
        if not isinstance(qm, dict):
            return {"active": False, "holding_reserve": False, "muted": False,
                    "robot_reachable": False, "pending_restore": False, "last_error": "",
                    "desired": self._desired}
        return {
            "active": bool(qm.get("active", False)),
            "holding_reserve": bool(qm.get("holding_reserve", False)),
            "muted": bool(qm.get("muted", False)),
            "robot_reachable": bool(qm.get("robot_reachable", False)),
            "pending_restore": bool(qm.get("pending_restore", False)),
            "last_error": str(qm.get("last_error") or ""),
            "desired": self._desired,
        }

    async def _apply_state(self, enabled: bool) -> None:
        res = await set_quiet_mode(
            self._data.session, self._data.base_url, self._data.serial, enabled)
        self._desired = bool(res.get("enabled", enabled))
        if isinstance(self.coordinator.data, dict):
            self.coordinator.data["quiet_mode"] = res
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        guard_not_cryosleep(self._data.manager, HomeAssistantError)
        self._data.manager.cancel_cycle("quiet mode set by hand")
        await self._apply_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        guard_not_cryosleep(self._data.manager, HomeAssistantError)
        self._data.manager.cancel_cycle("quiet mode set by hand")
        await self._apply_state(False)


class PhotoOnWakeSwitch(SwitchEntity, RestoreEntity):
    """When on, the sleep cycle stores one photo each time the robot is woken."""

    _attr_has_entity_name = True
    _attr_name = "Photo on wake"
    _attr_icon = "mdi:camera-timer"
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._manager = entry.runtime_data.manager
        self._cycle = self._manager.cycle
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_photo_on_wake"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        return not cryosleep_displaced(self._manager)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self._cycle.photo_on_wake = last is not None and last.state == "on"
        bind_cryosleep_entity(self, self._manager)

    @property
    def is_on(self) -> bool:
        return self._cycle.photo_on_wake

    async def async_turn_on(self, **kwargs: Any) -> None:
        guard_not_cryosleep(self._manager, HomeAssistantError)
        self._cycle.photo_on_wake = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        guard_not_cryosleep(self._manager, HomeAssistantError)
        self._cycle.photo_on_wake = False
        self.async_write_ha_state()


class MicMuteSwitch(SwitchEntity):
    """The robot's own microphone mute: while on, it hears no wake word and sends no voice.

    A change restarts the robot's programs (the pod does it so the robot takes the new state), so
    the camera and controls drop for a while and the robot wakes up; a running sleep cycle counts
    that as a wake. Unavailable unless the pod can log in to the robot (developer and OSKR robots).
    """

    _attr_has_entity_name = True
    _attr_name = "Mute microphones"
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._data = entry.runtime_data
        self._muted: bool | None = None
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_mic_mute"
        self._attr_device_info = device_info(entry)

    def _took(self, answer: dict | None) -> None:
        # The pod's 'restarting' is not kept: Home Assistant reads a polled entity again right
        # after each service call, so it would always show False.
        self._muted = None if answer is None else answer["muted"]

    @property
    def available(self) -> bool:
        return not cryosleep_displaced(self._data.manager) and self._muted is not None

    @property
    def is_on(self) -> bool | None:
        return self._muted

    @property
    def icon(self) -> str:
        return "mdi:microphone-off" if self._muted else "mdi:microphone"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        bind_cryosleep_entity(self, self._data.manager, refresh_on_exit=True)
        if getattr(self, "hass", None) is not None:
            @callback
            def _on_recovered() -> None:
                self.async_schedule_update_ha_state(True)

            self.async_on_remove(async_dispatcher_connect(
                self.hass, SIGNAL_RECOVERED.format(self._data.serial), _on_recovered))

    async def async_update(self) -> None:
        data = self._data
        if cryosleep_displaced(data.manager) and self._muted is not None:
            return
        coord_data = data.coordinator.data if data.coordinator is not None else None
        if isinstance(coord_data, dict) and coord_data.get("robot_reachable") is False:
            self._took(None)
            return
        try:
            self._took(await get_mic_mute(
                data.session, data.base_url, data.serial, timeout_s=POLL_TIMEOUT_S))
        except MIC_FAILURES:
            self._took(None)

    async def _set(self, muted: bool) -> None:
        data = self._data
        guard_not_cryosleep(data.manager, HomeAssistantError)
        try:
            answer = await set_mic_mute(data.session, data.base_url, data.serial, muted)
        except MIC_FAILURES as exc:
            raise HomeAssistantError(f"The microphones were not changed: {exc}") from exc
        if answer is None:
            raise HomeAssistantError("This robot or pod offers no microphone mute")
        self._took(answer)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

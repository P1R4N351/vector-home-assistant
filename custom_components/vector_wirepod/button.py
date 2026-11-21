"""Button entities for Vector actions and photo capture.

Each action sends a fixed intent or wake-word trigger through wire-pod (plus Photo capture).
Quiet mode holds the robot still, and a robot held still ignores intents and wake triggers
(measured 2026-11-19: the intent arrives and nothing happens). So:

  Dock               lifts quiet mode, asks for the charger, and puts quiet mode back once the
                     robot is on its charger, or after DOCK_WAIT_S if it never gets there. Ends
                     any sleep cycle.
  Sleep              starts the sleep cycle (sleepcycle.py): quiet mode off, sleep until woken,
                     then quiet mode for a set time, then sleep again.
  Explore            ends any sleep cycle, lifts quiet mode, sends explore_start, leaves quiet off.
  Listen for beat    ends any sleep cycle, lifts quiet mode, sends intent_imperative_dance.
  Go to sleep        ends any sleep cycle, lifts quiet mode, sends intent_system_sleep once.
  Fetch cube         ends any sleep cycle, lifts quiet mode, sends intent_imperative_fetchcube.
  Trigger wake word  ends any sleep cycle, lifts quiet mode, calls /api-sdk/trigger_wake_word.
  Take photo         stores one camera frame (photos.py). Quiet mode is lifted while capturing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import aiohttp

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .camera import device_info
from .const import CONF_SERIAL, DOMAIN, QUIET_SETTLE_S
from .cryosleep import bind_cryosleep_entity, cryosleep_displaced, guard_not_cryosleep
from .manual_buttons import manual_buttons
from .wirepod import (
    INTENT_DOCK,
    STIMULATE_BUTTON_SPECS,
    WAKE_WORD_BUTTON_SPEC,
    WirePodError,
    get_battery,
    send_intent,
    trigger_wake_word,
)

DOCK_WAIT_S = 180
DOCK_POLL_S = 6
FAILURES = (WirePodError, aiohttp.ClientError, TimeoutError, OSError, ValueError)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    assert entry is not None and callable(async_add_entities)
    entities: list[ButtonEntity] = [
        DockButton(entry),
        SleepButton(entry),
        *[StimulateIntentButton(entry, *spec) for spec in STIMULATE_BUTTON_SPECS],
        WakeWordButton(entry),
        PhotoButton(entry),
        *manual_buttons(hass, entry),
    ]
    async_add_entities(entities)


async def _run_action_leaving_quiet_off(data, manager, label: str, call: Callable[[], Awaitable[None]]) -> None:
    """End any sleep cycle, lift quiet mode if active, run action, and leave quiet mode off."""
    assert data is not None and manager is not None
    assert isinstance(label, str) and callable(call)
    guard_not_cryosleep(manager, HomeAssistantError)
    manager.cancel_cycle(f"{label.lower()} requested")
    quiet_was_on = bool(manager.quiet_is_on())
    try:
        if quiet_was_on:
            await manager.set_quiet(False)
            await asyncio.sleep(QUIET_SETTLE_S)
        await call()
    except FAILURES as exc:
        if quiet_was_on:
            await manager.set_quiet(True)
        raise HomeAssistantError(f"Vector did not take the {label} request: {exc}") from exc
    await data.coordinator.async_request_refresh()


class _VectorButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str, name: str, icon: str) -> None:
        self._data = entry.runtime_data
        self._manager = entry.runtime_data.manager
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.data[CONF_SERIAL]}_{key}"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        return not cryosleep_displaced(self._manager)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        bind_cryosleep_entity(self, self._manager)


class DockButton(_VectorButton):
    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "dock", "Dock", "mdi:ev-station")

    async def _restore_quiet_when_docked(self) -> None:
        """Wait for the charger (bounded), then switch quiet mode back on."""
        data = self._data
        for _ in range(DOCK_WAIT_S // DOCK_POLL_S):
            await asyncio.sleep(DOCK_POLL_S)
            try:
                battery = await get_battery(data.session, data.base_url, data.serial)
            except (WirePodError, aiohttp.ClientError, TimeoutError):
                continue
            if battery.get("is_on_charger_platform"):
                break
        try:
            await self._manager.set_quiet(True)
        except FAILURES:
            return
        await data.coordinator.async_request_refresh()

    async def async_press(self) -> None:
        data, manager = self._data, self._manager
        guard_not_cryosleep(manager, HomeAssistantError)
        manager.cancel_cycle("dock requested")
        was_quiet = bool(manager.quiet_is_on())
        try:
            if was_quiet:
                await manager.set_quiet(False)
                await asyncio.sleep(QUIET_SETTLE_S)
            await send_intent(data.session, data.base_url, data.serial, INTENT_DOCK)
        except FAILURES as exc:
            if was_quiet:
                await manager.set_quiet(True)
            raise HomeAssistantError(f"Vector did not take the Dock request: {exc}") from exc
        if was_quiet:
            self.hass.async_create_background_task(
                self._restore_quiet_when_docked(), f"{DOMAIN} restore quiet {data.serial}")
        await data.coordinator.async_request_refresh()


class SleepButton(_VectorButton):
    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "sleep", "Sleep", "mdi:power-sleep")

    async def async_press(self) -> None:
        guard_not_cryosleep(self._manager, HomeAssistantError)
        try:
            await self._manager.request_sleep()
        except FAILURES as exc:
            raise HomeAssistantError(f"Vector did not take the Sleep request: {exc}") from exc
        await self._data.coordinator.async_request_refresh()


class StimulateIntentButton(_VectorButton):
    """Send one fixed wire-pod stimulate intent when pressed."""

    def __init__(self, entry: ConfigEntry, key: str, name: str, icon: str, intent: str) -> None:
        assert isinstance(intent, str) and intent
        super().__init__(entry, key, name, icon)
        self._intent = intent

    async def async_press(self) -> None:
        """Send the configured intent after lifting quiet mode."""
        assert self._attr_name is not None and self._intent
        data = self._data
        await _run_action_leaving_quiet_off(
            data, self._manager, self._attr_name,
            lambda: send_intent(data.session, data.base_url, data.serial, self._intent))


class WakeWordButton(_VectorButton):
    """Trigger wire-pod's remote 'Hey Vector' wake word when pressed."""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, *WAKE_WORD_BUTTON_SPEC)

    async def async_press(self) -> None:
        """Trigger the remote wake word after lifting quiet mode."""
        assert self._attr_name is not None and self._data is not None
        data = self._data
        await _run_action_leaving_quiet_off(
            data, self._manager, self._attr_name,
            lambda: trigger_wake_word(data.session, data.base_url, data.serial))


class PhotoButton(_VectorButton):
    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "take_photo", "Take photo", "mdi:camera")

    async def async_press(self) -> None:
        guard_not_cryosleep(self._manager, HomeAssistantError)
        try:
            await self._manager.take_photo("manual")
        except FAILURES as exc:
            raise HomeAssistantError(f"No photo was taken: {exc}") from exc

"""Manual control of one Vector from Home Assistant, and the slow poll of its passed-through data.

controls.py decides what may be sent; this file holds behavior control while
it is used and lets it go after controls.IDLE_RELEASE_S without a command. It imports only the
parts of Home Assistant that every platform test provides, so any platform file can use it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import aiohttp

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import controls, robotdata
from .const import DOMAIN, QUIET_SETTLE_S, SIGNAL_RECOVERED
from .cryosleep import cryosleep_displaced, guard_not_cryosleep
from .wirepod import WirePodError, get_sdk_info

_LOGGER = logging.getLogger(__name__)
EXTRAS_POLL_S = 900
MAX_LISTENERS = 32
FAILURES = (WirePodError, aiohttp.ClientError, TimeoutError, OSError, ValueError)


class ManualControl:
    """Commands from the manual-control entities of one robot, one at a time."""

    def __init__(self, hass, session: aiohttp.ClientSession, base_url: str, serial: str,
                 manager, coordinator, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 idle_s: float = controls.IDLE_RELEASE_S,
                 grant_wait_s: float = controls.GRANT_WAIT_S) -> None:
        assert hass is not None and manager is not None and bool(serial)
        assert idle_s > 0 and grant_wait_s >= 0
        self.hass, self.manager, self.coordinator = hass, manager, coordinator
        self._session, self._base, self.serial = session, base_url, serial
        self._clock, self._sleep, self._grant_wait_s = clock, sleep, grant_wait_s
        self.hold = controls.ManualHold(idle_s=idle_s)
        self.head_deg: float | None = None
        self.lift_pct: float | None = None
        self.last_said: str | None = None
        self._timer = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsub_cryo = manager.add_cryosleep_listener(self._on_cryosleep)

    # ---- listeners ----------------------------------------------------------
    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        assert callable(callback) and len(self._listeners) < MAX_LISTENERS

        self._listeners.append(callback)

        def _remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _remove

    def _notify(self) -> None:
        for callback in list(self._listeners)[:MAX_LISTENERS]:
            callback()

    def notify(self) -> None:
        """Notify listening manual-control entities of updated local state."""
        self._notify()

    def displaced(self) -> bool:
        return cryosleep_displaced(self.manager)

    def _reachable(self) -> bool:
        data = getattr(self.coordinator, "data", None)
        return bool(data.get("robot_reachable")) if isinstance(data, dict) else False

    def seconds_until_release(self) -> int | None:
        due = self.hold.release_at()
        return None if due is None else max(0, round(due - self._clock()))

    def why_not(self) -> str | None:
        """Why a manual control cannot run now, or None."""
        return controls.refusal(self.manager.quiet_is_on(), self.displaced(), self._reachable())

    # ---- commands -----------------------------------------------------------
    async def _send(self, path: str, params: dict | None = None) -> dict:
        try:
            return await controls.send(self._session, self._base, self.serial, path, params)
        except controls.ControlRefused as exc:
            raise HomeAssistantError(f"Vector did not do it: {exc}") from exc
        except FAILURES as exc:
            raise HomeAssistantError(f"wire-pod did not take the command: {exc}") from exc

    async def _take(self) -> None:
        """Ask for control when it is not held. Must run under the lock."""
        if not self.hold.begin(self._clock()):
            return
        self.manager.cancel_cycle("manual control")
        try:
            await self._send(controls.PATH_ASSUME)
        except HomeAssistantError:
            self.hold.end()
            raise
        await self._sleep(self._grant_wait_s)

    async def command(self, path: str | None, params: dict | None = None) -> dict:
        """Run one command that needs behavior control (None only takes control)."""
        async with self._lock:
            why = self.why_not()
            if why is not None:
                raise HomeAssistantError(why)
            try:
                await self._take()
                return {"ok": True} if path is None else await self._send(path, params)
            finally:
                self._schedule_release()
                self._notify()

    async def _restore_quiet_after_say(self) -> None:
        self._cancel_timer()
        if self.hold.end():
            try:
                await controls.send(self._session, self._base, self.serial, controls.PATH_RELEASE)
            except FAILURES as exc:
                _LOGGER.warning("%s: releasing say control failed: %s", self.serial, exc)
        try:
            await self.manager.set_quiet(True)
        except FAILURES as exc:
            _LOGGER.warning("%s: restoring quiet mode after say failed: %s", self.serial, exc)
        changed = getattr(self.manager, "_changed", None)
        if callable(changed):
            changed()

    async def say(self, text: str) -> dict:
        """Speak text aloud; temporarily lift Quiet mode and restore it when done."""
        async with self._lock:
            guard_not_cryosleep(self.manager, HomeAssistantError)
            if not self._reachable():
                raise HomeAssistantError("Vector is not reachable through wire-pod")
            was_quiet = bool(self.manager.quiet_is_on())
            if was_quiet:
                try:
                    await self.manager.set_quiet(False)
                except FAILURES as exc:
                    raise HomeAssistantError(f"Quiet mode could not be lifted: {exc}") from exc
                await self._sleep(QUIET_SETTLE_S)
            try:
                await self._take()
                return await self._send(controls.PATH_SAY, {"text": text})
            finally:
                if was_quiet:
                    await self._restore_quiet_after_say()
                else:
                    self._schedule_release()
                self._notify()

    async def setting(self, path: str, params: dict) -> dict:
        """A change that needs no control (eye colour). Refused only during Cryosleep or offline."""
        async with self._lock:
            if self.displaced():
                raise HomeAssistantError("Cryosleep is on; turn it off first")
            if not self._reachable():
                raise HomeAssistantError("Vector is not reachable through wire-pod")
            return await self._send(path, params)

    async def stop(self) -> dict:
        """Stop the motors. Allowed whenever the robot is reachable."""
        async with self._lock:
            if not self._reachable():
                raise HomeAssistantError("Vector is not reachable through wire-pod")
            if self.hold.held:
                self.hold.begin(self._clock())
                self._schedule_release()
            return await self._send(controls.PATH_STOP)

    async def release(self) -> None:
        """Let go of behavior control if it is held. A failed release is logged, not raised."""
        async with self._lock:
            self._cancel_timer()
            if self.hold.end():
                try:
                    await controls.send(self._session, self._base, self.serial, controls.PATH_RELEASE)
                except FAILURES as exc:
                    _LOGGER.warning("%s: releasing manual control failed: %s", self.serial, exc)
            self._notify()

    # ---- the idle timer -----------------------------------------------------
    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_release(self) -> None:
        self._cancel_timer()
        due = self.hold.release_at()
        if due is not None:
            self._timer = self.hass.loop.call_later(max(0.0, due - self._clock()), self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        if self.hold.release_due(self._clock()):
            self.hass.async_create_background_task(self.release(), f"{DOMAIN} release {self.serial}")
        else:
            self._schedule_release()

    def _on_cryosleep(self) -> None:
        if self.displaced() and self.hold.held:
            self.hass.async_create_background_task(self.release(), f"{DOMAIN} release {self.serial}")
        self._notify()

    async def shutdown(self) -> None:
        self._unsub_cryo()
        await self.release()


async def read_extras(session: aiohttp.ClientSession, base: str, serial: str) -> dict:
    """Stats, settings and the robot's address. Each part may fail on its own."""
    out: dict = {"stats": None, "settings": None, "ip_address": None, "errors": []}
    try:
        out["stats"] = await robotdata.get_stats(session, base, serial)
    except FAILURES as exc:
        out["errors"].append(f"stats: {exc}"[:160])
    try:
        out["settings"] = await robotdata.get_settings(session, base, serial)
    except FAILURES as exc:
        out["errors"].append(f"settings: {exc}"[:160])
    try:
        robots = await get_sdk_info(session, base)
        ips = [r["ip_address"] for r in robots if r["esn"].lower() == serial.lower() and r["ip_address"]]
        out["ip_address"] = ips[0] if ips else None
    except FAILURES as exc:
        out["errors"].append(f"sdk info: {exc}"[:160])
    if out["stats"] is None and out["settings"] is None and out["ip_address"] is None:
        raise UpdateFailed("; ".join(out["errors"]) or "nothing could be read")
    return out


def ensure_control(hass, entry) -> ManualControl:
    """The robot's ManualControl, made by the first platform that needs it."""
    data = entry.runtime_data
    if getattr(data, "manual", None) is None:
        data.manual = ManualControl(hass, data.session, data.base_url, data.serial,
                                    data.manager, data.coordinator)
    return data.manual


def ensure_extras(hass, entry) -> DataUpdateCoordinator:
    """The robot's slow data coordinator, made and first refreshed by the first platform that needs it."""
    assert hass is not None and entry is not None
    data = entry.runtime_data
    assert bool(data.serial) and isinstance(data.base_url, str)
    if getattr(data, "extras", None) is None:
        async def _update() -> dict:
            return await read_extras(data.session, data.base_url, data.serial)

        data.extras = DataUpdateCoordinator(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {data.serial} data",
            update_interval=timedelta(seconds=EXTRAS_POLL_S), update_method=_update)
        @callback
        def _on_recovered() -> None:
            hass.async_create_background_task(
                data.extras.async_request_refresh(), f"{DOMAIN} data recover {data.serial}")

        unsub = async_dispatcher_connect(
            hass, SIGNAL_RECOVERED.format(data.serial), _on_recovered)
        on_unload = getattr(entry, "async_on_unload", None)
        if callable(on_unload):
            on_unload(unsub)
        hass.async_create_background_task(data.extras.async_refresh(), f"{DOMAIN} data {data.serial}")
    return data.extras

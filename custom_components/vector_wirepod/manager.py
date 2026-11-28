"""Carries out what the sleep cycle decides, and takes photos.

sleepcycle.py decides; this file acts, through the fixed calls in wirepod.py. One robot each.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import photos
from .const import (CONF_QUIET_HOLD_S, CONF_SERIAL, CONF_WIREPOD_URL, DEFAULT_QUIET_HOLD_S,
                    DOMAIN, EVENT_PHOTO, MAX_CRYOSLEEP_LISTENERS, MAX_QUIET_HOLD_S,
                    MIN_QUIET_HOLD_S, PHOTO_KEEP, PHOTO_SETTLE_FRAMES, PHOTO_WAIT_S,
                    QUIET_SETTLE_S, SIGNAL_PHOTO, SLEEP_SETTLE_S, WAKE_PHOTO_PAUSE_S,
                    WAKE_PHOTO_TRIES)
from .cryosleep import (CryosleepIO, CryosleepState, POLL_NOOP, execute_enter, execute_exit,
                        execute_poll_action)
from .sleepcycle import (ACT_PHOTO, ACT_QUIET_OFF, ACT_QUIET_ON, ACT_SLEEP, IDLE, SleepCycle)
from .wirepod import (CameraHub, INTENT_SLEEP, WirePodError, get_backlight, get_cryo_mute,
                      get_quiet_mode, get_robot_state, send_intent,
                      set_backlight, set_cryo_mute, set_quiet_mode)

_LOGGER = logging.getLogger(__name__)
MAX_ACTIONS = 4
FAILURES = (WirePodError, aiohttp.ClientError, TimeoutError, OSError, ValueError)


class VectorManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 session: aiohttp.ClientSession, hub: CameraHub) -> None:
        assert hass is not None and entry is not None and hub is not None
        self.hass = hass
        self.session = session
        self.hub = hub
        self.base_url: str = entry.data[CONF_WIREPOD_URL]
        self.serial: str = entry.data[CONF_SERIAL]
        hold = int(entry.options.get(CONF_QUIET_HOLD_S, DEFAULT_QUIET_HOLD_S))
        hold = max(MIN_QUIET_HOLD_S, min(MAX_QUIET_HOLD_S, hold))
        self.cycle = SleepCycle(quiet_hold_s=float(hold), settle_s=SLEEP_SETTLE_S)
        self.photo_dir: Path = photos.photo_dir(hass.config.config_dir, self.serial)
        self.last_photo: Path | None = None
        self.last_photo_error = ""
        self.coordinator = None          # set right after the coordinator is made
        self._lock = asyncio.Lock()
        self.cryosleep = CryosleepState()
        self._cryo_gen = 0
        self._manual_bl_gen = 0
        self._cryo_task: asyncio.Task | None = None
        self.cryosleep_busy = False
        self._cryo_lock = asyncio.Lock()
        self._cryo_listeners: list[Callable[[], None]] = []

    # ---- quiet mode ---------------------------------------------------------
    def quiet_is_on(self) -> bool | None:
        """Quiet mode as last read from the pod. None when the pod has no quiet mode."""
        data = self.coordinator.data if self.coordinator is not None else None
        quiet = data.get("quiet_mode") if isinstance(data, dict) else None
        return bool(quiet.get("enabled")) if isinstance(quiet, dict) else None

    async def set_quiet(self, on: bool) -> None:
        result = await set_quiet_mode(self.session, self.base_url, self.serial, on)
        data = self.coordinator.data if self.coordinator is not None else None
        if isinstance(data, dict):
            data["quiet_mode"] = result

    def _changed(self) -> None:
        if self.coordinator is not None:
            self.coordinator.async_update_listeners()

    # ---- sleep cycle --------------------------------------------------------
    def observe(self, robot_state: dict | None, robot_reachable: bool = True) -> None:
        """Feed one reading to Cryosleep or the regular sleep cycle."""
        asleep = robot_state.get("asleep") if isinstance(robot_state, dict) else None
        if self.cryosleep.enabled:
            if self.cryosleep_busy:
                return
            action = self.cryosleep.decide_poll(bool(robot_reachable), asleep, time.time())
            if action != POLL_NOOP:
                if action in ("mark_offline",):
                    self._notify_cryosleep()
                else:
                    self._cryo_gen += 1
                    self.cryosleep_busy = True
                    self._cryo_task = self.hass.async_create_background_task(
                        self._run_cryo_poll(action, self._cryo_gen),
                        f"{DOMAIN} cryosleep poll {self.serial}",
                    )
            return
        actions = self.cycle.observe(asleep, time.time())
        if actions:
            self.hass.async_create_background_task(
                self.run(actions), f"{DOMAIN} sleep cycle {self.serial}")

    # ---- cryosleep ----------------------------------------------------------
    def add_cryosleep_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register callback invoked whenever Cryosleep state changes. Returns remover."""
        assert callable(callback)
        assert len(self._cryo_listeners) < MAX_CRYOSLEEP_LISTENERS, "too many cryosleep listeners"
        self._cryo_listeners.append(callback)

        def _remove() -> None:
            if callback in self._cryo_listeners:
                self._cryo_listeners.remove(callback)

        return _remove

    def _notify_cryosleep(self) -> None:
        for callback in list(self._cryo_listeners)[:MAX_CRYOSLEEP_LISTENERS]:
            callback()
        self._changed()

    def _cryo_io(self) -> CryosleepIO:
        assert self.session is not None and bool(self.serial)
        assert isinstance(self.base_url, str) and bool(self.base_url)
        return CryosleepIO(
            get_quiet=lambda: get_quiet_mode(self.session, self.base_url, self.serial),
            set_quiet=self.set_quiet,
            get_backlight=lambda: get_backlight(self.session, self.base_url, self.serial),
            set_backlight=lambda lvl: set_backlight(self.session, self.base_url, self.serial, lvl),
            get_cryo_mute=lambda: get_cryo_mute(self.session, self.base_url, self.serial),
            set_cryo_mute=lambda muted: set_cryo_mute(self.session, self.base_url, self.serial, muted),
            get_robot_state=lambda: get_robot_state(self.session, self.base_url, self.serial),
            send_sleep=lambda: send_intent(self.session, self.base_url, self.serial, INTENT_SLEEP),
            sleep=asyncio.sleep,
            now=time.time,
            failures=FAILURES,
        )

    def _cancel_cryo_task(self) -> None:
        task = self._cryo_task
        if task is not None and not task.done():
            task.cancel()
        self._cryo_task = None

    def note_manual_backlight(self) -> None:
        """Mark a manual backlight write so an in-flight Cryosleep exit will not overwrite it."""
        self._manual_bl_gen = self._cryo_gen

    def start_cryosleep(self) -> asyncio.Task:
        """Displace active controls, mute robot/speaker, enforce sleep, and darken screen."""
        assert self.hass is not None and self.hub is not None
        self._cryo_gen += 1
        gen = self._cryo_gen
        self._cancel_cryo_task()
        self.cancel_cycle("cryosleep enabled")
        self.cryosleep.arm(self.hub.allowed)
        self.hub.set_allowed(False)
        self.cryosleep_busy = True
        self._notify_cryosleep()
        self._cryo_task = self.hass.async_create_background_task(
            self._run_cryo_enter(gen), f"{DOMAIN} cryosleep enter {self.serial}")
        return self._cryo_task

    def restore_cryosleep(self, attrs: dict | None) -> asyncio.Task:
        """Resume Cryosleep across a Home Assistant restart without clobbering saved values."""
        assert self.hass is not None and self.hub is not None
        self._cryo_gen += 1
        gen = self._cryo_gen
        self._cancel_cryo_task()
        self.cancel_cycle("cryosleep restored")
        self.cryosleep.restore_from_attrs(attrs, self.hub.allowed)
        self.hub.set_allowed(False)
        self.cryosleep_busy = True
        self._notify_cryosleep()
        self._cryo_task = self.hass.async_create_background_task(
            self._run_cryo_enter(gen), f"{DOMAIN} cryosleep resume {self.serial}")
        return self._cryo_task

    def stop_cryosleep(self) -> asyncio.Task:
        """Unlock displaced controls immediately and finish Cryosleep hardware restore in background."""
        assert self.hass is not None and self.hub is not None
        self._cryo_gen += 1
        gen = self._cryo_gen
        self._cancel_cryo_task()
        restore_cam = self.cryosleep.restore_camera_target()
        self.cryosleep.begin_exit()
        self.hub.set_allowed(restore_cam)
        self.cryosleep_busy = True
        self._notify_cryosleep()
        self._cryo_task = self.hass.async_create_background_task(
            self._run_cryo_exit(gen), f"{DOMAIN} cryosleep exit {self.serial}")
        return self._cryo_task

    async def _refresh_after_cryo(self, gen: int) -> None:
        if self._cryo_gen == gen:
            refresh = getattr(self.coordinator, "async_request_refresh", None)
            if callable(refresh):
                await refresh()

    async def _finish_cryo_task(self, gen: int, refresh: bool) -> None:
        if self._cryo_gen == gen:
            if self._cryo_task is asyncio.current_task():
                self._cryo_task = None
            self.cryosleep_busy = False
            self._notify_cryosleep()
            if refresh:
                await self._refresh_after_cryo(gen)

    async def _run_cryo_enter(self, gen: int) -> None:
        assert gen >= 1
        try:
            async with self._cryo_lock:
                if self._cryo_gen == gen:
                    await execute_enter(
                        self.cryosleep, self._cryo_io(), lambda: self._cryo_gen != gen,
                        known_asleep=self.robot_asleep(),
                    )
        except asyncio.CancelledError:
            if self._cryo_gen == gen:
                raise
        finally:
            await self._finish_cryo_task(gen, True)

    async def _run_cryo_poll(self, action: str, gen: int) -> None:
        assert gen >= 1 and bool(action)
        try:
            async with self._cryo_lock:
                if self._cryo_gen == gen:
                    await execute_poll_action(
                        action, self.cryosleep, self._cryo_io(), lambda: self._cryo_gen != gen)
        except asyncio.CancelledError:
            if self._cryo_gen == gen:
                raise
        finally:
            await self._finish_cryo_task(gen, action in ("lock_still", "settled_asleep", "reenforce_sleep"))

    async def _run_cryo_exit(self, gen: int) -> None:
        assert gen >= 1
        try:
            async with self._cryo_lock:
                if self._cryo_gen == gen:
                    await execute_exit(
                        self.cryosleep, self._cryo_io(), lambda: self._cryo_gen != gen,
                        skip_backlight=lambda: self._manual_bl_gen == gen,
                        known_asleep=self.robot_asleep(),
                    )
        except asyncio.CancelledError:
            if self._cryo_gen == gen:
                raise
        finally:
            await self._finish_cryo_task(gen, True)

    def cancel_cycle(self, why: str) -> None:
        if self.cycle.phase != IDLE:
            self.cycle.cancel(time.time(), why)
            self._changed()

    async def request_sleep(self) -> None:
        """A person asked for sleep. Raises when the robot did not take the request."""
        actions = self.cycle.request_sleep(time.time())
        try:
            await self.run(actions, strict=True)
        except FAILURES:
            self.cycle.cancel(time.time(), "the sleep request failed")
            self._changed()
            raise

    async def run(self, actions: list[str], strict: bool = False) -> None:
        """Carry out cycle actions in order. Unless strict, one failure does not stop the rest."""
        assert len(actions) <= MAX_ACTIONS
        async with self._lock:
            for action in actions[:MAX_ACTIONS]:
                try:
                    await self._do(action)
                except FAILURES as exc:
                    if strict:
                        raise
                    _LOGGER.warning("%s: sleep cycle step %s failed: %s", self.serial, action, exc)
        self._changed()

    async def _do(self, action: str) -> None:
        if action == ACT_QUIET_OFF:
            if self.quiet_is_on():
                await self.set_quiet(False)
                await asyncio.sleep(QUIET_SETTLE_S)
        elif action == ACT_QUIET_ON:
            if self.quiet_is_on() is not None:
                await self.set_quiet(True)
        elif action == ACT_SLEEP:
            await send_intent(self.session, self.base_url, self.serial, INTENT_SLEEP)
        elif action == ACT_PHOTO:
            await self._photo("wake")
        else:
            raise ValueError(f"unknown action {action!r}")

    # ---- photos -------------------------------------------------------------
    async def take_photo(self, reason: str) -> Path:
        """Take one photo now and store it. Raises when no fresh frame arrived."""
        async with self._lock:
            try:
                return await self._photo(reason)
            finally:
                self._changed()

    async def _photo(self, reason: str) -> Path:
        try:
            path = await self._photo_inner(reason)
        except FAILURES as exc:
            self.last_photo_error = str(exc)[:200]
            async_dispatcher_send(self.hass, SIGNAL_PHOTO.format(self.serial))
            raise
        self.last_photo_error = ""
        async_dispatcher_send(self.hass, SIGNAL_PHOTO.format(self.serial))
        return path

    def robot_asleep(self) -> bool | None:
        """The robot's own asleep flag as last read. None when it is not known."""
        data = self.coordinator.data if self.coordinator is not None else None
        state = data.get("robot_state") if isinstance(data, dict) else None
        return bool(state.get("asleep")) if isinstance(state, dict) else None

    async def _photo_inner(self, reason: str) -> Path:
        if not self.hub.allowed:
            raise WirePodError("the camera is switched off (Camera allowed)")
        if reason != "wake" and self.robot_asleep():
            # A sleeping robot gives no frames (measured 2026-11-19), and changing quiet mode
            # for a photo that cannot come would only risk waking it.
            raise WirePodError("the robot is asleep; a sleeping robot gives no camera frames")
        # Under quiet mode the camera gives no frames, so quiet mode is lifted for the moment
        # the photo takes and then put back.
        was_quiet = bool(self.quiet_is_on())
        if was_quiet:
            await self.set_quiet(False)
            await asyncio.sleep(QUIET_SETTLE_S)
        # Only the wake photo is asked for more than once: a person pressing the button gets a
        # prompt answer, while a robot that has just woken needs a few seconds before frames come.
        tries = WAKE_PHOTO_TRIES if reason == "wake" else 1
        try:
            frame = await photos.first_frame(self._fresh_frame, tries, WAKE_PHOTO_PAUSE_S, asyncio.sleep)
        finally:
            if was_quiet:
                await self.set_quiet(True)
        if frame is None:
            raise WirePodError("no camera frame arrived (robot asleep, or camera busy)")
        path = await self.hass.async_add_executor_job(
            photos.save_photo, self.photo_dir, frame, reason, None, PHOTO_KEEP)
        self.last_photo = path
        self.hass.bus.async_fire(EVENT_PHOTO, {
            "serial": self.serial, "reason": reason, "file": path.name})
        return path

    async def _fresh_frame(self) -> bytes | None:
        """A frame newer than anything already held, after a few frames for the exposure."""
        hub = self.hub
        hub.retry_now()
        lease = hub.acquire()
        try:
            got = await hub.next_frame(hub.seq, PHOTO_WAIT_S)
            if got is None:
                return None
            for _ in range(PHOTO_SETTLE_FRAMES):
                newer = await hub.next_frame(got[0], 1.0)
                if newer is None:
                    break
                got = newer
            return got[1]
        finally:
            lease.release()

"""Tests for 0.13.0: offline HA boot, primary/fallback failover, recovery signals, QuietMode desired restore."""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock

import aiohttp
from aiohttp import web

from test_v0120 import PKG, SERIAL, _install_extra_stubs

_install_extra_stubs()
CONST = importlib.import_module(f"{PKG}.const")
FAILOVER = importlib.import_module(f"{PKG}.failover")
SWITCH = importlib.import_module(f"{PKG}.switch")
LIGHT = importlib.import_module(f"{PKG}.light")
NUMBER = importlib.import_module(f"{PKG}.number")
CAMERA = importlib.import_module(f"{PKG}.camera")
MANUAL = importlib.import_module(f"{PKG}.manual")
INIT = importlib.import_module(f"{PKG}")


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    assert p > 0
    return p


class _RecorderCoordinator:
    def __init__(self, hass, logger, *, config_entry=None, name="", update_interval=None, update_method=None):
        assert hass is not None and update_method is not None
        self.hass = hass
        self.config_entry = config_entry
        self.name = name
        self.update_interval = update_interval
        self._update_method = update_method
        self.data = None
        self.last_update_success = False
        self._listeners: list = []

    def async_add_listener(self, cb, context=None):
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None

    def async_update_listeners(self) -> None:
        for cb in list(self._listeners):
            cb()

    async def async_refresh(self) -> None:
        try:
            self.data = await self._update_method()
            self.last_update_success = True
        except (INIT.UpdateFailed, aiohttp.ClientError, TimeoutError, RuntimeError):
            self.last_update_success = False
        self.async_update_listeners()

    async def async_request_refresh(self) -> None:
        await self.async_refresh()

    async def async_config_entry_first_refresh(self) -> None:
        await self.async_refresh()
        if not self.last_update_success:
            raise AssertionError("async_config_entry_first_refresh fails on pod outage")


class _DispatchHass:
    def __init__(self, config_dir: str, forwarded: list[list[str]]) -> None:
        self.config = types.SimpleNamespace(config_dir=config_dir)
        self.data: dict = {}
        self.tasks: list[asyncio.Task] = []
        self._forwarded = forwarded
        self.bus = types.SimpleNamespace(async_fire=lambda *a, **kw: None)

        async def _fwd(entry, platforms):
            self._forwarded.append(list(platforms))

        self.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=_fwd,
            async_unload_platforms=lambda e, p: asyncio.sleep(0, result=True),
            async_reload=lambda eid: asyncio.sleep(0),
        )

    def async_create_background_task(self, coro, name: str = "") -> asyncio.Task:
        t = asyncio.create_task(coro, name=name)
        self.tasks.append(t)
        return t

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _make_entry(url: str, fallback: str = "") -> object:
    unloads: list = []
    return types.SimpleNamespace(
        entry_id="entry_1",
        title="Vector Demo",
        data={CONST.CONF_WIREPOD_URL: url, CONST.CONF_SERIAL: SERIAL, CONST.CONF_FALLBACK_URL: fallback},
        options={},
        runtime_data=None,
        add_update_listener=lambda fn: (lambda: None),
        async_on_unload=lambda fn: unloads.append(fn),
    )


async def _start_pair_servers(p_down: list[bool]) -> tuple[web.AppRunner, web.AppRunner, str, str]:
    async def handle_primary(r: web.Request) -> web.Response:
        if p_down[0]:
            return web.Response(status=502, text="bad gateway")
        return web.json_response({"pod": "primary", "robots": []})

    a1, a2 = web.Application(), web.Application()
    a1.router.add_get("/api-sdk/get_sdk_info", handle_primary)
    a2.router.add_get("/api-sdk/get_sdk_info", lambda r: web.json_response({"pod": "fallback", "robots": []}))
    r1, r2 = web.AppRunner(a1), web.AppRunner(a2)
    await r1.setup()
    await r2.setup()
    await web.TCPSite(r1, "127.0.0.1", 0).start()
    await web.TCPSite(r2, "127.0.0.1", 0).start()
    return r1, r2, f"http://127.0.0.1:{r1.addresses[0][1]}", f"http://127.0.0.1:{r2.addresses[0][1]}"


async def _exercise_failover_cycle(fs: FAILOVER.FailoverSession, u1: str, now: list[float], p_down: list[bool]) -> dict:
    async with fs.get(f"{u1}/api-sdk/get_sdk_info") as resp:
        first = await resp.json()
    active_mid, failovers_mid = fs.active_url, fs.failovers
    p_down[0] = False
    now[0] = 10.0
    resp_direct = await fs.get(f"{u1}/api-sdk/get_sdk_info")
    second = await resp_direct.json()
    resp_direct.close()
    active_before_probe = fs.active_url
    now[0] = 61.0
    async with fs.get(f"{u1}/api-sdk/get_sdk_info") as resp:
        third = await resp.json()
    return {
        "first": first["pod"], "active_mid": active_mid, "failovers_mid": failovers_mid,
        "second": second["pod"], "active_before_probe": active_before_probe,
        "third": third["pod"], "active_end": fs.active_url, "failovers_end": fs.failovers,
    }


def _patch_ha_runtime(raw_session, signals: dict[str, list]) -> contextlib.ExitStack:
    assert raw_session is not None and isinstance(signals, dict)
    stack = contextlib.ExitStack()

    def _connect(hass, signal, target):
        is_cb = getattr(sys.modules["homeassistant.core"], "is_callback", None)
        if not (is_cb(target) if callable(is_cb) else bool(getattr(target, "_hass_callback", False))):
            raise AssertionError(f"dispatcher callback for {signal!r} is not @callback-decorated: {target!r}")
        signals.setdefault(signal, []).append(target)
        return lambda: signals[signal].remove(target) if target in signals.get(signal, ()) else None

    def _send(hass, signal, *args):
        for fn in list(signals.get(signal, ())):
            fn(*args)

    stack.enter_context(mock.patch.object(INIT, "async_get_clientsession", lambda hass: raw_session))
    stack.enter_context(mock.patch.object(INIT, "DataUpdateCoordinator", _RecorderCoordinator))
    stack.enter_context(mock.patch.object(MANUAL, "DataUpdateCoordinator", _RecorderCoordinator))
    stack.enter_context(mock.patch.object(INIT, "_sync_device_model", lambda *a, **kw: None))
    stack.enter_context(mock.patch.object(INIT, "async_dispatcher_send", _send))
    stack.enter_context(mock.patch.object(SWITCH, "async_dispatcher_connect", _connect))
    stack.enter_context(mock.patch.object(LIGHT, "async_dispatcher_connect", _connect))
    stack.enter_context(mock.patch.object(NUMBER, "async_dispatcher_connect", _connect))
    stack.enter_context(mock.patch.object(MANUAL, "async_dispatcher_connect", _connect))
    return stack


async def _bind_test_entity(entity, hass) -> list[int]:
    refreshes = [0]
    entity.hass = hass
    entity._on_remove = []
    entity.async_on_remove = lambda fn: entity._on_remove.append(fn)
    entity.async_write_ha_state = lambda: None
    entity.async_schedule_update_ha_state = lambda f=False: refreshes.__setitem__(0, refreshes[0] + int(bool(f)))
    await entity.async_added_to_hass()
    return refreshes


def _start_recovery_app(pod_online: list[bool], quiet_state: list[bool], quiet_posts: list[bool]) -> web.Application:
    async def h_quiet(r: web.Request) -> web.Response:
        if not pod_online[0]:
            raise web.HTTPBadGateway()
        if r.method == "POST":
            val = r.query.get("enable") == "true"
            quiet_state[0] = val
            quiet_posts.append(val)
        return web.json_response({"enabled": quiet_state[0], "active": quiet_state[0], "muted": quiet_state[0], "robot_reachable": True})

    async def h_battery(r: web.Request) -> web.Response:
        if not pod_online[0]:
            raise web.HTTPBadGateway()
        return web.json_response({"battery_level": 3, "battery_volts": 4.1, "is_on_charger_platform": True})

    app = web.Application()
    app.router.add_route("*", "/api-sdk/quiet_mode", h_quiet)
    app.router.add_get("/api-sdk/get_battery", h_battery)
    app.router.add_get("/api-sdk/robot_state", lambda r: web.json_response({"ok": True, "asleep": True}))
    return app


class FailoverUnitTests(unittest.TestCase):
    def test_url_normalization(self) -> None:
        self.assertEqual(FAILOVER.normalize_pod_url("  http://wirepod.local:8080/  "), "http://wirepod.local:8080")
        self.assertEqual(FAILOVER.normalize_pod_url("", allow_empty=True), "")
        for bad in ("", "ftp://pod:8080", "pod:8080", 123):
            with self.assertRaises(ValueError):
                FAILOVER.normalize_pod_url(bad)

    def test_client_error_fails_over_to_live_fallback(self) -> None:
        async def run() -> tuple[dict, str, int]:
            app = web.Application()
            app.router.add_get("/api-sdk/get_sdk_info", lambda r: web.json_response({"pod": "fallback", "robots": []}))
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            fb, dead = f"http://127.0.0.1:{runner.addresses[0][1]}", f"http://127.0.0.1:{_closed_port()}"
            try:
                async with aiohttp.ClientSession() as raw:
                    fs = FAILOVER.FailoverSession(raw, dead, fb)
                    async with fs.get(f"{dead}/api-sdk/get_sdk_info") as resp:
                        return await resp.json(), fs.active_url, fs.failovers
            finally:
                await runner.cleanup()

        body, active, failovers = asyncio.run(run())
        self.assertEqual(body["pod"], "fallback")
        self.assertEqual(failovers, 1)
        self.assertTrue(active.startswith("http://127.0.0.1:"))

    def test_failover_on_502_and_primary_recovery(self) -> None:
        now, p_down = [0.0], [True]

        async def run() -> dict:
            r1, r2, u1, u2 = await _start_pair_servers(p_down)
            try:
                async with aiohttp.ClientSession() as raw:
                    fs = FAILOVER.FailoverSession(raw, u1, u2, clock=lambda: now[0], primary_retry_s=60.0)
                    out = await _exercise_failover_cycle(fs, u1, now, p_down)
                    return {**out, "u1": u1, "u2": u2}
            finally:
                await r1.cleanup()
                await r2.cleanup()

        out = asyncio.run(run())
        self.assertEqual((out["first"], out["second"], out["third"]), ("fallback", "fallback", "primary"))
        self.assertEqual((out["active_mid"], out["active_end"]), (out["u2"], out["u1"]))
        self.assertEqual((out["failovers_mid"], out["failovers_end"]), (1, 2))


class OfflineBootAndRecoveryTests(unittest.TestCase):
    def test_setup_while_pod_down_restores_quiet_desired_and_recovers(self) -> None:
        pod_online, quiet_state, quiet_posts, signals = [False], [False], [], {}

        async def run() -> dict:
            runner = web.AppRunner(_start_recovery_app(pod_online, quiet_state, quiet_posts))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            fb_url, dead_url = f"http://127.0.0.1:{runner.addresses[0][1]}", f"http://127.0.0.1:{_closed_port()}"
            forwarded: list[list[str]] = []
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    async with aiohttp.ClientSession() as raw:
                        with _patch_ha_runtime(raw, signals):
                            hass, entry = _DispatchHass(tmp, forwarded), _make_entry(dead_url, fb_url)
                            ok = await INIT.async_setup_entry(hass, entry)
                            coord = entry.runtime_data.coordinator
                            boot_ok = coord.last_update_success
                            bl_ref = await _bind_test_entity(LIGHT.FaceBacklight(entry), hass)
                            vol_ref = await _bind_test_entity(NUMBER.VectorVolumeNumber(entry), hass)
                            mic_ref = await _bind_test_entity(SWITCH.MicMuteSwitch(entry), hass)
                            extras = MANUAL.ensure_extras(hass, entry)
                            qm = SWITCH.QuietModeSwitch(entry)
                            qm.async_get_last_state = lambda: asyncio.sleep(
                                0, result=types.SimpleNamespace(state="unavailable", attributes={"desired": True}))
                            await _bind_test_entity(qm, hass)
                            desired_attr = qm.extra_state_attributes["desired"]
                            pod_online[0] = True
                            await coord.async_refresh()
                            if hass.tasks:
                                await asyncio.gather(*hass.tasks)
                            cam_url = CAMERA.VectorCamera(entry).extra_state_attributes["active_pod_url"]
                            return {"ok": ok, "fwd": forwarded, "boot": boot_ok, "des": desired_attr,
                                    "posts": quiet_posts, "url": cam_url, "fb": fb_url,
                                    "bl": bl_ref[0], "vol": vol_ref[0], "mic": mic_ref[0],
                                    "extras_tasks": len(hass.tasks), "has_extras": extras is not None}
                finally:
                    await runner.cleanup()

        out = asyncio.run(run())
        self.assertTrue(out["ok"] and out["des"] and not out["boot"])
        self.assertEqual((out["fwd"], out["posts"], out["url"]), ([CONST.PLATFORMS], [True], out["fb"]))
        self.assertGreaterEqual(min(out["bl"], out["vol"], out["mic"]), 1)
        self.assertTrue(out["has_extras"] and out["extras_tasks"] >= 2)
        self.assertIn(CONST.SIGNAL_RECOVERED.format(SERIAL), signals)


if __name__ == "__main__":
    unittest.main()

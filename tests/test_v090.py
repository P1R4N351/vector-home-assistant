"""Tests for the 0.9.0 addition: Vector master volume slider (get_volume, set_volume, number.py)."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

POD = "http://pod:8080"


def _stub_class(name: str, is_exc: bool = False) -> type:
    assert isinstance(name, str) and name
    base = Exception if is_exc else object
    return type(name, (base,), {})


def _make_number_mode() -> type:
    class _NumberMode:
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    assert _NumberMode.SLIDER == "slider"
    return _NumberMode


def _install_missing_ha_stubs() -> None:
    specs = {
        "homeassistant": {},
        "homeassistant.components": {},
        "homeassistant.components.camera": {
            "Camera": _stub_class("Camera"),
            "async_get_still_stream": lambda *a, **kw: None,
        },
        "homeassistant.components.number": {
            "NumberEntity": _stub_class("NumberEntity"),
            "NumberMode": _make_number_mode(),
        },
        "homeassistant.config_entries": {"ConfigEntry": _stub_class("ConfigEntry")},
        "homeassistant.core": {
            "HomeAssistant": _stub_class("HomeAssistant"),
            "callback": lambda fn: (setattr(fn, "_hass_callback", True) or fn),
        },
        "homeassistant.exceptions": {"HomeAssistantError": _stub_class("HomeAssistantError", True)},
        "homeassistant.helpers": {},
        "homeassistant.helpers.aiohttp_client": {"async_get_clientsession": lambda *a, **kw: None},
        "homeassistant.helpers.device_registry": {"DeviceInfo": dict},
        "homeassistant.helpers.dispatcher": {
            "async_dispatcher_send": lambda *a, **kw: None,
            "async_dispatcher_connect": lambda *a, **kw: (lambda: None),
        },
        "homeassistant.helpers.entity_platform": {
            "AddConfigEntryEntitiesCallback": _stub_class("AddConfigEntryEntitiesCallback"),
        },
        "homeassistant.helpers.event": {},
        "homeassistant.helpers.restore_state": {"RestoreEntity": _stub_class("RestoreEntity")},
        "homeassistant.helpers.update_coordinator": {
            "CoordinatorEntity": _stub_class("CoordinatorEntity"),
            "DataUpdateCoordinator": _stub_class("DataUpdateCoordinator"),
            "UpdateFailed": _stub_class("UpdateFailed", True),
        },
    }
    assert "homeassistant.components.number" in specs
    for mod_name, attrs in specs.items():
        if mod_name in sys.modules:
            continue
        mod = types.ModuleType(mod_name)
        for attr, val in attrs.items():
            setattr(mod, attr, val)
        sys.modules[mod_name] = mod
    sys.modules["homeassistant.helpers"].device_registry = sys.modules["homeassistant.helpers.device_registry"]


def _load_number_module():
    try:
        importlib.import_module("homeassistant.components.number")
    except ModuleNotFoundError:
        _install_missing_ha_stubs()
    const_mod = importlib.import_module("custom_components.vector_wirepod.const")
    wirepod_mod = importlib.import_module("custom_components.vector_wirepod.wirepod")
    number_mod = importlib.import_module("custom_components.vector_wirepod.number")
    num_comp = importlib.import_module("homeassistant.components.number")
    exc_mod = importlib.import_module("homeassistant.exceptions")
    assert hasattr(number_mod, "VectorVolumeNumber")
    return const_mod, wirepod_mod, number_mod, num_comp.NumberMode, exc_mod.HomeAssistantError


CONST, WIREPOD, NUMBER, NUMBER_MODE, HA_ERROR = _load_number_module()


class _FakeHub:
    def frame_size(self) -> None:
        return None


class _FakeCoordinator:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data


class _FakeManager:
    """0.12.0: what manual control asks of the manager."""

    cryosleep = None

    def quiet_is_on(self) -> bool:
        return False

    def cancel_cycle(self, why: str) -> None:
        assert why

    def add_cryosleep_listener(self, callback):
        assert callable(callback)
        return lambda: None


class _FakeData:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, serial: str,
                 coord_data: dict | None = None) -> None:
        self.session = session
        self.manager = _FakeManager()
        self.base_url = base_url
        self.serial = serial
        self.coordinator = _FakeCoordinator(coord_data)
        self.hub = _FakeHub()


class _FakeEntry:
    def __init__(self, data_obj: _FakeData) -> None:
        self.title = "Vector DEMO"
        self.data = {"serial": data_obj.serial, "wirepod_url": data_obj.base_url}
        self.runtime_data = data_obj


def _make_volume_app(seen: list, state: dict):
    assert isinstance(seen, list) and isinstance(state, dict)

    async def handle_settings(request: web.Request) -> web.Response:
        serial = request.query.get("serial")
        seen.append(("get", request.method, serial, None))
        if serial == "40400000":
            return web.Response(status=404, text="not found")
        if serial == "50200000":
            return web.Response(status=502, text="bad gateway")
        if serial == "off00000":
            return web.Response(
                status=200,
                text='error: rpc error: code = Unavailable desc = connection error: desc = "no route to host"',
            )
        if serial == "bad00000":
            return web.Response(status=200, text="{not-json")
        if serial in ("bool0000", "high0000", "low00000", "miss0000"):
            vals = {"bool0000": True, "high0000": 6, "low00000": -1}
            body = {} if serial == "miss0000" else {"master_volume": vals[serial]}
            return web.json_response(body)
        return web.json_response({"master_volume": state["volume"], "eye_color": 4})

    async def handle_volume(request: web.Request) -> web.Response:
        serial = request.query.get("serial")
        vol = request.query.get("volume")
        seen.append(("set", request.method, serial, vol))
        if state.get("set_err_text"):
            return web.Response(status=200, text="error: robot offline")
        if state.get("set_http_fail"):
            return web.Response(status=500, text="failed")
        state["volume"] = int(vol)
        return web.Response(status=200, text="done")

    app = web.Application()
    app.router.add_get("/api-sdk/get_sdk_settings", handle_settings)
    app.router.add_post("/api-sdk/volume", handle_volume)
    return app


class VolumeUrlTests(unittest.TestCase):
    def test_valid_urls_and_platform(self) -> None:
        self.assertIn("number", CONST.PLATFORMS)
        self.assertEqual(
            WIREPOD.build_url(POD, "/api-sdk/get_sdk_settings", "0dd1cafe"),
            f"{POD}/api-sdk/get_sdk_settings?serial=0dd1cafe",
        )
        for level in range(WIREPOD.VOLUME_MIN, WIREPOD.VOLUME_MAX + 1):
            self.assertEqual(
                WIREPOD.build_url(POD, "/api-sdk/volume", "0dd1cafe", volume=level),
                f"{POD}/api-sdk/volume?serial=0dd1cafe&volume={level}",
            )

    def test_volume_url_validation(self) -> None:
        for bad in (-1, 6, True, False, 2.5, "3"):
            with self.assertRaises(ValueError):
                WIREPOD.build_url(POD, "/api-sdk/volume", "0dd1cafe", volume=bad)
        for bad_call in (
            ("/api-sdk/get_sdk_settings", None, None),
            ("/api-sdk/volume", None, 3),
            ("/api-sdk/volume", "0dd1cafe", None),
            ("/api-sdk/quiet_mode", "0dd1cafe", 3),
        ):
            with self.assertRaises(ValueError):
                WIREPOD.build_url(POD, bad_call[0], bad_call[1], volume=bad_call[2])


class VolumeHttpTests(unittest.TestCase):
    def test_get_volume_success_and_errors(self) -> None:
        seen, state = [], {"volume": 3}

        async def run() -> tuple:
            runner = web.AppRunner(_make_volume_app(seen, state))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                async with aiohttp.ClientSession() as session:
                    got = await WIREPOD.get_volume(session, base, "0dd1cafe")
                    missing = await WIREPOD.get_volume(session, base, "40400000")
                    errs = []
                    for ser in ("off00000", "50200000", "bad00000", "bool0000", "high0000", "low00000", "miss0000"):
                        with self.assertRaises(WIREPOD.WirePodError) as ctx:
                            await WIREPOD.get_volume(session, base, ser)
                        errs.append(str(ctx.exception))
                    return got, missing, errs
            finally:
                await runner.cleanup()

        got, missing, errs = asyncio.run(run())
        self.assertEqual(got, 3)
        self.assertIsNone(missing)
        self.assertIn("no route to host", errs[0])
        self.assertNotIn("not JSON", errs[0])
        self.assertEqual(len(errs), 7)

    def test_set_volume_success_and_errors(self) -> None:
        seen, state = [], {"volume": 0}

        async def run() -> None:
            runner = web.AppRunner(_make_volume_app(seen, state))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                async with aiohttp.ClientSession() as session:
                    await WIREPOD.set_volume(session, base, "0dd1cafe", 4)
                    self.assertEqual(await WIREPOD.get_volume(session, base, "0dd1cafe"), 4)
                    state["set_err_text"] = True
                    with self.assertRaises(WIREPOD.WirePodError):
                        await WIREPOD.set_volume(session, base, "0dd1cafe", 2)
                    state.update({"set_err_text": False, "set_http_fail": True})
                    with self.assertRaises(WIREPOD.WirePodError):
                        await WIREPOD.set_volume(session, base, "0dd1cafe", 2)
            finally:
                await runner.cleanup()

        asyncio.run(run())
        self.assertEqual(seen[:2], [("set", "POST", "0dd1cafe", "4"), ("get", "GET", "0dd1cafe", None)])


class VolumeEntityTests(unittest.TestCase):
    def test_setup_registers_volume_slider(self) -> None:
        async def run() -> tuple:
            async with aiohttp.ClientSession() as session:
                entry = _FakeEntry(_FakeData(session, POD, "0dd1cafe"))
                captured = []
                await NUMBER.async_setup_entry(object(), entry, lambda ents, update_before_add=False: captured.append((ents, update_before_add)))
                assert len(captured) == 2        # the volume slider, then the manual sliders (0.12.0)
                assert [e._attr_unique_id for e in captured[1][0]] == ["0dd1cafe_manual_head_angle", "0dd1cafe_manual_lift_height"]
                return captured[0]

        entities, update_before_add = asyncio.run(run())
        self.assertTrue(update_before_add)
        self.assertEqual(len(entities), 1)
        ent = entities[0]
        self.assertEqual((ent._attr_unique_id, ent._attr_name), ("0dd1cafe_volume", "Volume"))
        self.assertEqual(ent._attr_mode, NUMBER_MODE.SLIDER)
        self.assertEqual((ent._attr_native_min_value, ent._attr_native_max_value, ent._attr_native_step), (0.0, 5.0, 1.0))

    def test_update_icons_and_unreachable_skip(self) -> None:
        seen, state = [], {"volume": 0}

        async def run() -> dict:
            runner = web.AppRunner(_make_volume_app(seen, state))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                async with aiohttp.ClientSession() as session:
                    data = _FakeData(session, base, "0dd1cafe", {"robot_reachable": True})
                    ent = NUMBER.VectorVolumeNumber( _FakeEntry(data) )
                    snaps = []
                    for vol in (0, 1, 3, 5):
                        state["volume"] = vol
                        await ent.async_update()
                        snaps.append((ent.available, ent.native_value, ent.icon, ent.extra_state_attributes["level_name"]))
                    data.coordinator.data = {"robot_reachable": False}
                    before_offline = len(seen)
                    await ent.async_update()
                    return {"snaps": snaps, "offline_avail": ent.available, "offline_val": ent.native_value, "polls_added": len(seen) - before_offline}
            finally:
                await runner.cleanup()

        out = asyncio.run(run())
        self.assertEqual(
            out["snaps"],
            [
                (True, 0.0, "mdi:volume-mute", "mute"),
                (True, 1.0, "mdi:volume-low", "low"),
                (True, 3.0, "mdi:volume-medium", "medium"),
                (True, 5.0, "mdi:volume-high", "high"),
            ],
        )
        self.assertFalse(out["offline_avail"])
        self.assertIsNone(out["offline_val"])
        self.assertEqual(out["polls_added"], 0)

    def test_set_value_writes_state_and_handles_errors(self) -> None:
        seen, state, writes = [], {"volume": 2}, []

        async def run() -> tuple:
            runner = web.AppRunner(_make_volume_app(seen, state))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                async with aiohttp.ClientSession() as session:
                    ent = NUMBER.VectorVolumeNumber(_FakeEntry(_FakeData(session, base, "0dd1cafe")))
                    ent.async_write_ha_state = lambda: writes.append(ent.native_value)
                    await ent.async_update()
                    await ent.async_set_native_value(5.0)
                    state["set_http_fail"] = True
                    with self.assertRaises(HA_ERROR):
                        await ent.async_set_native_value(1.0)
                    for bad in (-1.0, 6.0, True):
                        with self.assertRaises(HA_ERROR):
                            await ent.async_set_native_value(bad)
                    return ent.native_value, state["volume"], list(writes)
            finally:
                await runner.cleanup()

        val, pod_vol, written = asyncio.run(run())
        self.assertEqual((val, pod_vol), (5.0, 5))
        self.assertEqual(written, [5.0])


if __name__ == "__main__":
    unittest.main()

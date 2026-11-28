"""Tests for 0.14.0: offline poll skip, 8s poll timeout, slider/Say restore, and HA services."""
from __future__ import annotations

import asyncio
import importlib
import types
import unittest
from unittest import mock

from aiohttp import web

from test_v0120 import PKG, SERIAL, _install_extra_stubs

_install_extra_stubs()
CONST = importlib.import_module(f"{PKG}.const")
CONTROLS = importlib.import_module(f"{PKG}.controls")
SWITCH = importlib.import_module(f"{PKG}.switch")
LIGHT = importlib.import_module(f"{PKG}.light")
NUMBER = importlib.import_module(f"{PKG}.number")
MNUMBERS = importlib.import_module(f"{PKG}.manual_numbers")
TEXT = importlib.import_module(f"{PKG}.text")
MANUAL = importlib.import_module(f"{PKG}.manual")
SERVICES = importlib.import_module(f"{PKG}.services")
HA_ERROR = importlib.import_module("homeassistant.exceptions").HomeAssistantError


def _fake_runtime_entry(serial: str = SERIAL, *, reachable: bool = True,
                        quiet: bool = False, cryo: bool = False) -> object:
    mgr = types.SimpleNamespace(
        cryosleep=types.SimpleNamespace(enabled=cryo, holding_reserve=False, saved_backlight=None),
        quiet_is_on=lambda: quiet,
        add_cryosleep_listener=lambda fn: (lambda: None),
        cancel_cycle=lambda reason: None,
        set_quiet=mock.AsyncMock(),
    )
    coord = types.SimpleNamespace(data={"robot_reachable": reachable})
    rt = types.SimpleNamespace(
        session=object(),
        base_url="http://pod:8080",
        serial=serial,
        hub=types.SimpleNamespace(frame_size=lambda: None),
        manager=mgr,
        coordinator=coord,
        manual=None,
    )
    return types.SimpleNamespace(
        entry_id=f"entry_{serial}",
        title=f"Vector {serial}",
        data={CONST.CONF_WIREPOD_URL: "http://pod:8080", CONST.CONF_SERIAL: serial},
        options={},
        runtime_data=rt,
    )


class _FakeServices:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], object] = {}
        self.schemas: dict[tuple[str, str], object] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.handlers

    def async_register(self, domain: str, service: str, handler, schema=None) -> None:
        self.handlers[(domain, service)] = handler
        self.schemas[(domain, service)] = schema

    def async_remove(self, domain: str, service: str) -> None:
        self.handlers.pop((domain, service), None)
        self.schemas.pop((domain, service), None)


def _fake_hass(entries: list[object]) -> object:
    return types.SimpleNamespace(
        services=_FakeServices(),
        loop=types.SimpleNamespace(call_later=lambda delay, cb: types.SimpleNamespace(cancel=lambda: None)),
        config_entries=types.SimpleNamespace(async_entries=lambda domain: list(entries)),
    )


class PollTimeoutAndOfflineSkipTests(unittest.TestCase):
    def test_poll_timeout_under_ha_warning_threshold(self) -> None:
        self.assertLessEqual(CONST.POLL_TIMEOUT_S, 9.0)
        self.assertGreater(CONST.POLL_TIMEOUT_S, 0.0)

    def test_mic_mute_and_backlight_skip_when_unreachable(self) -> None:
        entry = _fake_runtime_entry(reachable=False)
        mic = SWITCH.MicMuteSwitch(entry)
        mic._muted = True
        light = LIGHT.FaceBacklight(entry)
        light._level = 8

        with mock.patch.object(SWITCH, "get_mic_mute", side_effect=AssertionError("should skip")) as gm,              mock.patch.object(LIGHT, "get_backlight", side_effect=AssertionError("should skip")) as gb:
            asyncio.run(mic.async_update())
            asyncio.run(light.async_update())

        self.assertEqual(gm.call_count, 0)
        self.assertEqual(gb.call_count, 0)
        self.assertIsNone(mic.is_on)
        self.assertFalse(mic.available)
        self.assertIsNone(light.is_on)
        self.assertFalse(light.available)

    def test_polling_entities_pass_poll_timeout_when_reachable(self) -> None:
        entry = _fake_runtime_entry(reachable=True)
        mic = SWITCH.MicMuteSwitch(entry)
        light = LIGHT.FaceBacklight(entry)
        vol = NUMBER.VectorVolumeNumber(entry)

        with mock.patch.object(SWITCH, "get_mic_mute", new=mock.AsyncMock(return_value={"muted": False, "restarting": False})) as gm,              mock.patch.object(LIGHT, "get_backlight", new=mock.AsyncMock(return_value=10)) as gb,              mock.patch.object(NUMBER, "get_volume", new=mock.AsyncMock(return_value=3)) as gv:
            asyncio.run(mic.async_update())
            asyncio.run(light.async_update())
            asyncio.run(vol.async_update())

        self.assertEqual(gm.call_args.kwargs.get("timeout_s"), CONST.POLL_TIMEOUT_S)
        self.assertEqual(gb.call_args.kwargs.get("timeout_s"), CONST.POLL_TIMEOUT_S)
        self.assertEqual(gv.call_args.kwargs.get("timeout_s"), CONST.POLL_TIMEOUT_S)


class RestoreStateTests(unittest.TestCase):
    def test_manual_numbers_restore_valid_and_reject_invalid(self) -> None:
        entry = _fake_runtime_entry()
        hass = _fake_hass([entry])
        head, lift = MNUMBERS.manual_numbers(hass, entry)

        with mock.patch.object(head, "async_get_last_state", new=mock.AsyncMock(
                return_value=types.SimpleNamespace(state="18.5", attributes={}))),              mock.patch.object(lift, "async_get_last_state", new=mock.AsyncMock(
                 return_value=types.SimpleNamespace(state="65", attributes={}))):
            asyncio.run(head.async_added_to_hass())
            asyncio.run(lift.async_added_to_hass())

        self.assertEqual(head.native_value, 18.5)
        self.assertEqual(lift.native_value, 65.0)

        entry2 = _fake_runtime_entry(serial="bad00001")
        head2, lift2 = MNUMBERS.manual_numbers(hass, entry2)
        with mock.patch.object(head2, "async_get_last_state", new=mock.AsyncMock(
                return_value=types.SimpleNamespace(state="nan", attributes={}))),              mock.patch.object(lift2, "async_get_last_state", new=mock.AsyncMock(
                 return_value=types.SimpleNamespace(state="200", attributes={}))):
            asyncio.run(head2.async_added_to_hass())
            asyncio.run(lift2.async_added_to_hass())

        self.assertIsNone(head2.native_value)
        self.assertIsNone(lift2.native_value)

    def test_say_text_restores_last_said_attribute(self) -> None:
        entry = _fake_runtime_entry()
        hass = _fake_hass([entry])
        ctrl = MANUAL.ensure_control(hass, entry)
        say = TEXT.SayText(entry, ctrl)

        with mock.patch.object(say, "async_get_last_state", new=mock.AsyncMock(
                return_value=types.SimpleNamespace(state="", attributes={"last_said": "hello bot"}))):
            asyncio.run(say.async_added_to_hass())

        self.assertEqual(say.native_value, "")
        self.assertEqual(say.extra_state_attributes, {"last_said": "hello bot"})


class ServiceTests(unittest.TestCase):
    def test_services_drive_say_head_lift_turn_stop_and_multi_robot(self) -> None:
        seen: list[str] = []

        async def handle(r: web.Request) -> web.Response:
            seen.append(r.path_qs)
            return web.Response(text="success")

        async def run_flow() -> None:
            app = web.Application()
            app.router.add_get("/api-sdk/{tail:.*}", handle)
            app.router.add_post("/api-sdk/{tail:.*}", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = runner.addresses[0][1]
            try:
                import aiohttp
                async with aiohttp.ClientSession() as sess:
                    e1 = _fake_runtime_entry("0dd1cafe", reachable=True)
                    e1.runtime_data.session = sess
                    e1.runtime_data.base_url = f"http://127.0.0.1:{port}"
                    e1.runtime_data.manual = MANUAL.ManualControl(
                        _fake_hass([e1]), sess, e1.runtime_data.base_url, "0dd1cafe",
                        e1.runtime_data.manager, e1.runtime_data.coordinator, grant_wait_s=0.0)
                    hass = _fake_hass([e1])
                    SERVICES.async_register_services(hass)
                    for name in SERVICES.ALL_SERVICES:
                        self.assertTrue(hass.services.has_service(CONST.DOMAIN, name))

                    call = lambda name, data: hass.services.handlers[(CONST.DOMAIN, name)](
                        types.SimpleNamespace(data=data))

                    await call("say", {"text": "hi there"})
                    self.assertEqual(e1.runtime_data.manual.last_said, "hi there")
                    await call("drive", {"distance_mm": 100, "speed_mmps": 75})
                    await call("turn", {"angle_deg": -45, "speed_dps": 90})
                    await call("set_head", {"angle_deg": 15})
                    self.assertEqual(e1.runtime_data.manual.head_deg, 15.0)
                    await call("set_lift", {"height_pct": 50})
                    self.assertEqual(e1.runtime_data.manual.lift_pct, 50.0)
                    await call("stop", {})

                    e2 = _fake_runtime_entry("0dd2beef", reachable=True)
                    hass2 = _fake_hass([e1, e2])
                    SERVICES.async_register_services(hass2)
                    with self.assertRaises(HA_ERROR):
                        await hass2.services.handlers[(CONST.DOMAIN, "stop")](types.SimpleNamespace(data={}))
                    await hass2.services.handlers[(CONST.DOMAIN, "stop")](
                        types.SimpleNamespace(data={"serial": "0DD1CAFE"}))

                    SERVICES.async_unregister_services(hass2, e1.entry_id)
                    self.assertTrue(hass2.services.has_service(CONST.DOMAIN, "say"))
                    SERVICES.async_unregister_services(hass, e1.entry_id)
                    self.assertFalse(hass.services.has_service(CONST.DOMAIN, "say"))
            finally:
                await runner.cleanup()

        asyncio.run(run_flow())
        self.assertTrue(any("/api-sdk/say_text?" in p for p in seen))
        self.assertTrue(any("/api-sdk/drive_straight?" in p for p in seen))
        self.assertTrue(any("/api-sdk/turn_in_place?" in p for p in seen))
        self.assertTrue(any("/api-sdk/set_head_angle?" in p for p in seen))
        self.assertTrue(any("/api-sdk/set_lift_height?" in p for p in seen))
        self.assertTrue(any("/api-sdk/stop_motors?" in p for p in seen))

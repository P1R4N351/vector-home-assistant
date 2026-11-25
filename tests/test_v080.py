"""Tests for the 0.8.0 additions: wire-pod stimulate actions and HA button entities."""
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


def _install_ha_stubs() -> None:
    assert "homeassistant" not in sys.modules
    specs = {
        "homeassistant": {},
        "homeassistant.components": {},
        "homeassistant.components.button": {"ButtonEntity": _stub_class("ButtonEntity")},
        "homeassistant.components.camera": {
            "Camera": _stub_class("Camera"),
            "async_get_still_stream": lambda *a, **kw: None,
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
    assert "homeassistant.exceptions" in specs
    for mod_name, attrs in specs.items():
        mod = types.ModuleType(mod_name)
        for attr, val in attrs.items():
            setattr(mod, attr, val)
        sys.modules[mod_name] = mod
    sys.modules["homeassistant.helpers"].device_registry = sys.modules["homeassistant.helpers.device_registry"]


def _load_button_module():
    try:
        importlib.import_module("homeassistant.components.button")
    except ModuleNotFoundError:
        _install_ha_stubs()
    wirepod_mod = importlib.import_module("custom_components.vector_wirepod.wirepod")
    button_mod = importlib.import_module("custom_components.vector_wirepod.button")
    exc_mod = importlib.import_module("homeassistant.exceptions")
    assert hasattr(button_mod, "async_setup_entry")
    assert hasattr(exc_mod, "HomeAssistantError")
    return wirepod_mod, button_mod, exc_mod.HomeAssistantError


WIREPOD, BUTTON, HA_ERROR = _load_button_module()


class _FakeHub:
    def frame_size(self) -> None:
        return None


class _FakeCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class _FakeManager:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.calls: list[tuple[str, object]] = []

    def quiet_is_on(self) -> bool:
        return self.quiet

    async def set_quiet(self, on: bool) -> None:
        assert isinstance(on, bool)
        self.quiet = on
        self.calls.append(("set_quiet", on))

    def cancel_cycle(self, why: str) -> None:
        assert isinstance(why, str) and why
        self.calls.append(("cancel_cycle", why))

    def add_cryosleep_listener(self, callback):  # 0.12.0: manual control listens for Cryosleep
        assert callable(callback)
        return lambda: None


class _FakeData:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, serial: str,
                 manager: _FakeManager, coordinator: _FakeCoordinator) -> None:
        self.session = session
        self.base_url = base_url
        self.serial = serial
        self.manager = manager
        self.coordinator = coordinator
        self.hub = _FakeHub()


class _FakeEntry:
    def __init__(self, data_obj: _FakeData) -> None:
        self.title = "Vector DEMO"
        self.data = {"serial": data_obj.serial, "wirepod_url": data_obj.base_url}
        self.runtime_data = data_obj


def _make_pod_app(seen: list, fail_flag: dict):
    assert isinstance(seen, list) and isinstance(fail_flag, dict)

    async def handle_intent(request: web.Request) -> web.Response:
        seen.append(("intent", request.method, request.query.get("serial"), request.query.get("intent")))
        if fail_flag.get("fail"):
            return web.Response(status=500, text="intent failed")
        return web.Response(status=200, text="done")

    async def handle_wake(request: web.Request) -> web.Response:
        seen.append(("wake", request.method, request.query.get("serial")))
        if fail_flag.get("bad_body"):
            return web.Response(status=200, text="done")
        if fail_flag.get("fail"):
            return web.Response(status=500, text="consolevar error")
        return web.Response(status=200, text="success")

    app = web.Application()
    app.router.add_get("/api-sdk/cloud_intent", handle_intent)
    app.router.add_post("/api-sdk/trigger_wake_word", handle_wake)
    return app


class StimulateUrlTests(unittest.TestCase):
    def test_allowed_intents_urls(self) -> None:
        expected = (
            ("intent_system_charger", f"{POD}/api-sdk/cloud_intent?serial=0dd1cafe&intent=intent_system_charger"),
            ("intent_system_sleep", f"{POD}/api-sdk/cloud_intent?serial=0dd1cafe&intent=intent_system_sleep"),
            ("explore_start", f"{POD}/api-sdk/cloud_intent?serial=0dd1cafe&intent=explore_start"),
            ("intent_imperative_dance", f"{POD}/api-sdk/cloud_intent?serial=0dd1cafe&intent=intent_imperative_dance"),
            ("intent_imperative_fetchcube", f"{POD}/api-sdk/cloud_intent?serial=0dd1cafe&intent=intent_imperative_fetchcube"),
        )
        self.assertEqual(len(WIREPOD.ALLOWED_INTENTS), len(expected))
        for intent, url in expected:
            self.assertEqual(WIREPOD.build_url(POD, "/api-sdk/cloud_intent", "0dd1cafe", intent=intent), url)

    def test_rejected_intents_and_paths(self) -> None:
        for bad in ("intent_explore_start", "explore", "", "intent_imperative_abuse"):
            with self.assertRaises(ValueError):
                WIREPOD.build_url(POD, "/api-sdk/cloud_intent", "0dd1cafe", intent=bad)
        with self.assertRaises(ValueError):
            WIREPOD.build_url(POD, "/api-sdk/cloud_intent", None, intent=WIREPOD.INTENT_EXPLORE)
        with self.assertRaises(ValueError):
            WIREPOD.build_url(POD, "/api-sdk/get_battery", "0dd1cafe", intent=WIREPOD.INTENT_EXPLORE)

    def test_wake_word_url_validation(self) -> None:
        self.assertEqual(
            WIREPOD.build_url(POD, "/api-sdk/trigger_wake_word", "0dd1cafe"),
            f"{POD}/api-sdk/trigger_wake_word?serial=0dd1cafe",
        )
        with self.assertRaises(ValueError):
            WIREPOD.build_url(POD, "/api-sdk/trigger_wake_word", None)
        with self.assertRaises(ValueError):
            WIREPOD.build_url(POD, "/api-sdk/trigger_wake_word", "0dd1cafe", intent=WIREPOD.INTENT_EXPLORE)


class StimulateHttpTests(unittest.TestCase):
    def test_send_intent_all_stimulate_actions(self) -> None:
        seen, fail_flag = [], {"fail": False}

        async def run() -> None:
            runner = web.AppRunner(_make_pod_app(seen, fail_flag))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                async with aiohttp.ClientSession() as session:
                    for _, _, _, intent in WIREPOD.STIMULATE_BUTTON_SPECS:
                        await WIREPOD.send_intent(session, base, "0dd1cafe", intent)
                    fail_flag["fail"] = True
                    with self.assertRaises(WIREPOD.WirePodError):
                        await WIREPOD.send_intent(session, base, "0dd1cafe", WIREPOD.INTENT_EXPLORE)
            finally:
                await runner.cleanup()

        asyncio.run(run())
        self.assertEqual(
            [item[3] for item in seen[:4]],
            ["explore_start", "intent_imperative_dance", "intent_system_sleep", "intent_imperative_fetchcube"],
        )
        self.assertTrue(all(item[1] == "GET" and item[2] == "0dd1cafe" for item in seen))

    def test_trigger_wake_word_over_http(self) -> None:
        seen, fail_flag = [], {"fail": False, "bad_body": False}

        async def run() -> None:
            runner = web.AppRunner(_make_pod_app(seen, fail_flag))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            try:
                async with aiohttp.ClientSession() as session:
                    await WIREPOD.trigger_wake_word(session, base, "0dd1cafe")
                    fail_flag["bad_body"] = True
                    with self.assertRaises(WIREPOD.WirePodError):
                        await WIREPOD.trigger_wake_word(session, base, "0dd1cafe")
                    fail_flag.update({"bad_body": False, "fail": True})
                    with self.assertRaises(WIREPOD.WirePodError):
                        await WIREPOD.trigger_wake_word(session, base, "0dd1cafe")
            finally:
                await runner.cleanup()

        asyncio.run(run())
        self.assertEqual(seen, [("wake", "POST", "0dd1cafe")] * 3)


async def _press_stimulate_buttons(fail_mode: bool, quiet_start: bool) -> dict:
    assert isinstance(fail_mode, bool) and isinstance(quiet_start, bool)
    seen, fail_flag, old_settle = [], {"fail": fail_mode}, BUTTON.QUIET_SETTLE_S
    BUTTON.QUIET_SETTLE_S = 0.0
    runner = web.AppRunner(_make_pod_app(seen, fail_flag))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    records = []
    try:
        async with aiohttp.ClientSession() as session:
            manager, coord = _FakeManager(quiet_start), _FakeCoordinator()
            entry = _FakeEntry(_FakeData(session, base, "0dd1cafe", manager, coord))
            entities = []
            await BUTTON.async_setup_entry(object(), entry, entities.extend)
            for btn in entities[2:7]:
                manager.quiet = quiet_start
                manager.calls.clear()
                coord.refreshes = 0
                err = None
                try:
                    await btn.async_press()
                except HA_ERROR as exc:
                    err = exc
                records.append((btn._attr_name, manager.quiet, list(manager.calls), coord.refreshes, err))
    finally:
        BUTTON.QUIET_SETTLE_S = old_settle
        await runner.cleanup()
    assert len(records) == 5
    return {"records": records, "seen": seen}


class ButtonEntityTests(unittest.TestCase):
    def test_setup_registers_all_buttons(self) -> None:
        async def run() -> list:
            async with aiohttp.ClientSession() as session:
                entry = _FakeEntry(_FakeData(session, POD, "0dd1cafe", _FakeManager(False), _FakeCoordinator()))
                created = []
                await BUTTON.async_setup_entry(object(), entry, created.extend)
                assert len(created) == 13
                return created

        entities = asyncio.run(run())
        self.assertEqual(
            [(e._attr_unique_id, e._attr_name) for e in entities],
            [
                ("0dd1cafe_dock", "Dock"),
                ("0dd1cafe_sleep", "Sleep"),
                ("0dd1cafe_explore", "Explore"),
                ("0dd1cafe_listen_for_beat", "Listen for beat"),
                ("0dd1cafe_go_to_sleep", "Go to sleep"),
                ("0dd1cafe_fetch_cube", "Fetch cube"),
                ("0dd1cafe_trigger_wake_word", "Trigger wake word"),
                ("0dd1cafe_take_photo", "Take photo"),
                ("0dd1cafe_manual_forward", "Drive forward"),
                ("0dd1cafe_manual_backward", "Drive backward"),
                ("0dd1cafe_manual_turn_left", "Turn left"),
                ("0dd1cafe_manual_turn_right", "Turn right"),
                ("0dd1cafe_manual_stop", "Stop"),
            ],
        )

    def test_stimulate_buttons_lift_and_leave_quiet_off(self) -> None:
        out = asyncio.run(_press_stimulate_buttons(fail_mode=False, quiet_start=True))
        for _, quiet_after, calls, refreshes, err in out["records"]:
            self.assertIsNone(err)
            self.assertFalse(quiet_after)
            self.assertEqual([c[0] for c in calls], ["cancel_cycle", "set_quiet"])
            self.assertEqual(calls[1][1], False)
            self.assertEqual(refreshes, 1)
        self.assertEqual(
            out["seen"],
            [
                ("intent", "GET", "0dd1cafe", "explore_start"),
                ("intent", "GET", "0dd1cafe", "intent_imperative_dance"),
                ("intent", "GET", "0dd1cafe", "intent_system_sleep"),
                ("intent", "GET", "0dd1cafe", "intent_imperative_fetchcube"),
                ("wake", "POST", "0dd1cafe"),
            ],
        )

    def test_stimulate_buttons_skip_set_quiet_when_already_off(self) -> None:
        out = asyncio.run(_press_stimulate_buttons(fail_mode=False, quiet_start=False))
        for _, quiet_after, calls, refreshes, err in out["records"]:
            self.assertIsNone(err)
            self.assertFalse(quiet_after)
            self.assertEqual([c[0] for c in calls], ["cancel_cycle"])
            self.assertEqual(refreshes, 1)
        self.assertEqual(len(out["seen"]), 5)

    def test_stimulate_buttons_restore_quiet_on_failure(self) -> None:
        out = asyncio.run(_press_stimulate_buttons(fail_mode=True, quiet_start=True))
        for _, quiet_after, calls, refreshes, err in out["records"]:
            self.assertIsInstance(err, HA_ERROR)
            self.assertTrue(quiet_after)
            self.assertEqual([c[0] for c in calls], ["cancel_cycle", "set_quiet", "set_quiet"])
            self.assertEqual(calls[1:], [("set_quiet", False), ("set_quiet", True)])
            self.assertEqual(refreshes, 0)


if __name__ == "__main__":
    unittest.main()

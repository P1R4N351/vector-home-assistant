"""Tests for 0.12.0: the rename, passed-through robot data, and manual controls."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest

import aiohttp
from aiohttp import web

from test_v0100 import HA_ERROR, MODS  # installs the base Home Assistant stand-ins when HA is absent

SERIAL = "0dd1cafe"
# Measured on one robot through wire-pod, 2026-11-20.
DEMO_STATS = {"Alive.seconds": 51732922, "BStat.ReactedToTriggerWord": 219, "FeatureType.Utility": 31,
              "Odom.Body": 212088151195, "Pet.ms": 3216945, "Stim.CumlPosDelta": 1118425}
DEMO_SETTINGS = {"button_wakeword": 0, "clock_24_hour": True,
                 "custom_eye_color": {"enabled": True, "hue": 0.79699999094009399, "saturation": 0.10999999940395355},
                 "default_location": "Springfield", "dist_is_metric": True, "eye_color": 4, "locale": "en-US",
                 "master_volume": 0, "temp_is_fahrenheit": False, "time_zone": "Etc/UTC"}


def _enum(name: str, **members: str) -> type:
    return type(name, (), dict(members))


def _install_extra_stubs() -> None:
    """Stand-ins for the parts of Home Assistant only 0.12.0 uses, added only when HA is absent."""
    try:
        importlib.import_module("homeassistant.components.text")
        return
    except ModuleNotFoundError:
        pass
    base = MODS["switch"].SwitchEntity.__mro__[1]
    coord = sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity
    specs = {
        "homeassistant.components.text": {"TextEntity": type("TextEntity", (base,), {}),
                                          "TextMode": _enum("TextMode", TEXT="text", PASSWORD="password")},
        "homeassistant.components.select": {"SelectEntity": type("SelectEntity", (base,), {})},
        "homeassistant.components.sensor": {
            "SensorEntity": type("SensorEntity", (base,), {}),
            "SensorDeviceClass": _enum("SensorDeviceClass", DURATION="duration", DISTANCE="distance",
                                       VOLTAGE="voltage", ENUM="enum"),
            "SensorStateClass": _enum("SensorStateClass", MEASUREMENT="measurement",
                                      TOTAL_INCREASING="total_increasing")},
        "homeassistant.components.binary_sensor": {
            "BinarySensorEntity": type("BinarySensorEntity", (base,), {}),
            "BinarySensorDeviceClass": _enum("BinarySensorDeviceClass", MOVING="moving", PLUG="plug",
                                             BATTERY_CHARGING="battery_charging", CONNECTIVITY="connectivity")},
        "homeassistant.const": {"EntityCategory": _enum("EntityCategory", DIAGNOSTIC="diagnostic"),
                                "UnitOfTime": _enum("UnitOfTime", SECONDS="s", MINUTES="min", DAYS="d"),
                                "UnitOfLength": _enum("UnitOfLength", METERS="m"),
                                "UnitOfElectricPotential": _enum("UnitOfElectricPotential", VOLT="V")},
    }
    assert coord is not None
    for name, attrs in specs.items():
        mod = sys.modules.get(name) or types.ModuleType(name)
        for attr, val in attrs.items():
            if not hasattr(mod, attr):
                setattr(mod, attr, val)
        sys.modules[name] = mod


_install_extra_stubs()
PKG = "custom_components.vector_wirepod"
CONTROLS = importlib.import_module(f"{PKG}.controls")
ROBOTDATA = importlib.import_module(f"{PKG}.robotdata")
MANUAL = importlib.import_module(f"{PKG}.manual")
MBUTTONS = importlib.import_module(f"{PKG}.manual_buttons")
MNUMBERS = importlib.import_module(f"{PKG}.manual_numbers")
MSWITCH = importlib.import_module(f"{PKG}.manual_switch")
TEXT = importlib.import_module(f"{PKG}.text")
SELECT = importlib.import_module(f"{PKG}.select")
EXTRAS = importlib.import_module(f"{PKG}.extras")
WIREPOD = MODS["wirepod"]
POD = "http://pod:8080"


class ControlUrlTests(unittest.TestCase):
    def test_each_path_builds_exactly(self) -> None:
        u = CONTROLS.control_url
        self.assertEqual(u(POD, CONTROLS.PATH_ASSUME, SERIAL),
                         f"{POD}/api-sdk/assume_behavior_control?serial={SERIAL}&priority=default")
        self.assertEqual(u(POD, CONTROLS.PATH_DRIVE, SERIAL, {"dist_mm": 100, "speed_mmps": 75}),
                         f"{POD}/api-sdk/drive_straight?serial={SERIAL}&dist_mm=100.0&speed_mmps=75.0")
        self.assertEqual(u(POD, CONTROLS.PATH_TURN, SERIAL, {"angle_deg": -45, "speed_dps": 90}),
                         f"{POD}/api-sdk/turn_in_place?serial={SERIAL}&angle_deg=-45.0&speed_dps=90.0")
        self.assertEqual(u(POD, CONTROLS.PATH_HEAD, SERIAL, {"angle_deg": -22}),
                         f"{POD}/api-sdk/set_head_angle?serial={SERIAL}&angle_deg=-22.0")
        self.assertEqual(u(POD, CONTROLS.PATH_LIFT, SERIAL, {"height_pct": 100}),
                         f"{POD}/api-sdk/set_lift_height?serial={SERIAL}&height_pct=100.0")
        self.assertEqual(u(POD, CONTROLS.PATH_EYE, SERIAL, {"color": 6}),
                         f"{POD}/api-sdk/eye_color?serial={SERIAL}&color=6")
        self.assertEqual(u(POD, CONTROLS.PATH_EYE_CUSTOM, SERIAL, {"hue": 0.5, "sat": 1}),
                         f"{POD}/api-sdk/custom_eye_color?serial={SERIAL}&hue=0.5&sat=1.0")
        self.assertEqual(u(POD, CONTROLS.PATH_STOP, SERIAL), f"{POD}/api-sdk/stop_motors?serial={SERIAL}")
        self.assertEqual(u(POD, CONTROLS.PATH_SAY, SERIAL, {"text": " Tom & Jerry? #1 é "}),
                         f"{POD}/api-sdk/say_text?serial={SERIAL}&text=Tom%20%26%20Jerry%3F%20%231%20%C3%A9")

    def test_never_override_priority(self) -> None:
        self.assertEqual(CONTROLS.PRIORITY, "default")
        with self.assertRaises(ValueError):
            CONTROLS.control_url(POD, CONTROLS.PATH_ASSUME, SERIAL, {"priority": "override_behaviors"})

    def test_refuses_what_is_not_allowed(self) -> None:
        bad = [
            ("/api-sdk/move_wheels", {"lw": 1, "rw": 1}), ("/api-sdk/cloud_intent", {}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": 600.5, "speed_mmps": 75}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": -150.5, "speed_mmps": 75}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": 10, "speed_mmps": 161}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": 10}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": 10, "speed_mmps": 75, "x": 1}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": True, "speed_mmps": 75}),
            (CONTROLS.PATH_DRIVE, {"dist_mm": float("nan"), "speed_mmps": 75}),
            (CONTROLS.PATH_TURN, {"angle_deg": 361, "speed_dps": 90}),
            (CONTROLS.PATH_HEAD, {"angle_deg": 45.5}), (CONTROLS.PATH_HEAD, {"angle_deg": -22.5}),
            (CONTROLS.PATH_LIFT, {"height_pct": 100.5}), (CONTROLS.PATH_LIFT, {"height_pct": -0.5}),
            (CONTROLS.PATH_EYE, {"color": 7}), (CONTROLS.PATH_EYE, {"color": -1}),
            (CONTROLS.PATH_EYE, {"color": 2.5}), (CONTROLS.PATH_EYE, {"color": "4"}),
            (CONTROLS.PATH_EYE_CUSTOM, {"hue": 1.01, "sat": 0.5}),
            (CONTROLS.PATH_EYE_CUSTOM, {"hue": 0.5, "sat": -0.01}),
            (CONTROLS.PATH_STOP, {"x": 1}), (CONTROLS.PATH_SAY, {}),
            (CONTROLS.PATH_SAY, {"text": ""}), (CONTROLS.PATH_SAY, {"text": "   "}),
            (CONTROLS.PATH_SAY, {"text": "a" * 256}), (CONTROLS.PATH_SAY, {"text": "hi\nthere"}),
            (CONTROLS.PATH_SAY, {"text": 5}),
        ]
        for path, params in bad:
            with self.assertRaises(ValueError, msg=f"{path} {params}"):
                CONTROLS.control_url(POD, path, SERIAL, params)
        for base, serial in (("ftp://pod", SERIAL), (POD, "0dd1&x=1"), (POD, "")):
            with self.assertRaises(ValueError):
                CONTROLS.control_url(base, CONTROLS.PATH_STOP, serial)
        self.assertEqual(len(CONTROLS.check_say_text("a" * 255)), 255)


class AnswerTests(unittest.TestCase):
    def test_answers(self) -> None:
        pa = CONTROLS.parse_answer
        self.assertEqual(pa(200, '{"ok": true, "dist_mm": 50}', "drive")["dist_mm"], 50)
        self.assertEqual(pa(200, "success", "say"), {"ok": True, "answer": "success"})
        with self.assertRaises(CONTROLS.ControlRefused) as ctx:
            pa(200, "error: on_charger", "drive")
        self.assertIn("charger", str(ctx.exception))
        with self.assertRaises(CONTROLS.ControlRefused) as ctx:
            pa(200, "error: cliff_lockout (cliff_detected)", "drive")
        self.assertIn("edge", str(ctx.exception))
        with self.assertRaises(CONTROLS.ControlRefused):
            pa(200, '{"ok": false, "error": "picked_up"}', "drive")
        with self.assertRaises(WIREPOD.WirePodError) as ctx:
            pa(500, "boom", "drive")
        self.assertNotIsInstance(ctx.exception, CONTROLS.ControlRefused)
        self.assertEqual(CONTROLS.reason_text("error: something new"), "something new")

    def test_refusal_order(self) -> None:
        r = CONTROLS.refusal
        self.assertIn("Cryosleep", r(True, True, False))
        self.assertIn("Quiet mode", r(True, False, False))
        self.assertIn("not reachable", r(False, False, False))
        self.assertIsNone(r(False, False, True))
        self.assertIsNone(r(None, False, True))

    def test_hold(self) -> None:
        h = CONTROLS.ManualHold(idle_s=60.0)
        self.assertIsNone(h.release_at())
        self.assertTrue(h.begin(10.0))
        self.assertFalse(h.begin(20.0))
        self.assertEqual(h.release_at(), 80.0)
        self.assertFalse(h.release_due(79.9))
        self.assertTrue(h.release_due(80.0))
        self.assertTrue(h.end())
        self.assertFalse(h.end())
        self.assertFalse(h.release_due(1000.0))


class RobotDataTests(unittest.TestCase):
    def test_stats_units(self) -> None:
        s = ROBOTDATA.normalize_stats(DEMO_STATS)
        self.assertEqual(s["distance_m"], 2120.88)       # 212088151195 / 100000 mm
        self.assertEqual(s["time_on_s"], 51732922)
        self.assertEqual(s["petted_s"], 3216.9)
        self.assertEqual(s["wake_word_reactions"], 219)
        self.assertEqual(len(s["counters"]), 6)

    def test_stats_drops_what_is_not_a_number(self) -> None:
        s = ROBOTDATA.normalize_stats({"a": True, "b": "7", "c": 2, "": 1, "x" * 81: 1})
        self.assertEqual(s["counters"], {"c": 2.0})
        self.assertIsNone(s["distance_m"])
        big = ROBOTDATA.normalize_stats({f"k{i}": i for i in range(500)})
        self.assertEqual(len(big["counters"]), ROBOTDATA.MAX_STAT_KEYS)
        with self.assertRaises(WIREPOD.WirePodError):
            ROBOTDATA.normalize_stats([1, 2])

    def test_settings(self) -> None:
        s = ROBOTDATA.normalize_settings(DEMO_SETTINGS)
        self.assertEqual(s["eye_color"], "Custom")
        self.assertEqual(s["eye_preset"], 4)
        self.assertAlmostEqual(s["custom_hue"], 0.797, places=3)
        self.assertEqual((s["locale"], s["time_zone"], s["default_location"]),
                         ("en-US", "Etc/UTC", "Springfield"))
        self.assertEqual((s["clock_24_hour"], s["temp_is_fahrenheit"]), (True, False))
        off = dict(DEMO_SETTINGS, custom_eye_color={"enabled": False, "hue": 0.1, "saturation": 0.2})
        self.assertEqual(ROBOTDATA.normalize_settings(off)["eye_color"], "Sapphire")
        self.assertIsNone(ROBOTDATA.normalize_settings({"eye_color": 9})["eye_color"])
        self.assertIsNone(ROBOTDATA.normalize_settings({"eye_color": True})["eye_color"])
        self.assertIsNone(ROBOTDATA.normalize_settings({"locale": 5})["locale"])
        self.assertEqual(ROBOTDATA.EYE_PRESETS[4], "Sapphire")
        self.assertEqual(len(ROBOTDATA.EYE_PRESETS), CONTROLS.EYE_PRESET_MAX + 1)

    def test_stats_url(self) -> None:
        self.assertEqual(ROBOTDATA.stats_url(POD, SERIAL), f"{POD}/api-sdk/get_robot_stats?serial={SERIAL}")
        for base, serial in (("file:///x", SERIAL), (POD, "a b")):
            with self.assertRaises(ValueError):
                ROBOTDATA.stats_url(base, serial)


class _FakePod:
    """A wire-pod stand-in that records every request and answers from a table."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []
        self.answers: dict[str, tuple[int, str]] = {}

    async def handle(self, request: web.Request) -> web.Response:
        self.seen.append((request.path, dict(request.query)))
        status, text = self.answers.get(request.path, (200, '{"ok": true}'))
        return web.Response(status=status, text=text)

    def paths(self) -> list[str]:
        return [p.rsplit("/", 1)[-1] for p, _ in self.seen]


class _FakeLoop:
    def __init__(self) -> None:
        self.timers: list[list] = []

    def call_later(self, delay: float, callback):
        timer = [delay, callback, False]
        self.timers.append(timer)
        return types.SimpleNamespace(cancel=lambda: timer.__setitem__(2, True))


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()
        self.tasks: list = []

    def async_create_background_task(self, coro, name: str):
        assert name
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class _Cryo:
    def __init__(self) -> None:
        self.enabled = False
        self.phase = "idle"


class _Manager:
    def __init__(self) -> None:
        self.quiet = False
        self.cryosleep = _Cryo()
        self.cancelled: list[str] = []
        self.listeners: list = []
        self.quiet_calls: list[bool] = []
        self.changed_calls = 0

    def quiet_is_on(self) -> bool:
        return self.quiet

    async def set_quiet(self, on: bool) -> None:
        self.quiet_calls.append(on)
        self.quiet = on

    def _changed(self) -> None:
        self.changed_calls += 1

    def cancel_cycle(self, why: str) -> None:
        self.cancelled.append(why)

    def add_cryosleep_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)


async def _with_pod(body):
    pod = _FakePod()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", pod.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        async with aiohttp.ClientSession() as session:
            return await body(pod, base, session)
    finally:
        await runner.cleanup()


def _control(session, base, manager, reachable=True, clock=None):
    hass = _FakeHass()
    coord = types.SimpleNamespace(data={"robot_reachable": reachable})
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    now = clock or (lambda: 1000.0)
    ctl = MANUAL.ManualControl(hass, session, base, SERIAL, manager, coord, clock=now, sleep=sleep,
                               idle_s=60.0, grant_wait_s=1.5)
    return ctl, hass, coord, slept


class ManualControlTests(unittest.TestCase):
    def test_first_command_takes_control_once(self) -> None:
        async def body(pod, base, session):
            mgr = _Manager()
            ctl, hass, _, slept = _control(session, base, mgr)
            path, params = MBUTTONS.step_params("forward")
            await ctl.command(path, params)
            await ctl.command(*MBUTTONS.step_params("turn_left"))
            return pod, mgr, ctl, hass, slept
        pod, mgr, ctl, hass, slept = asyncio.run(_with_pod(body))
        self.assertEqual(pod.paths(), ["assume_behavior_control", "drive_straight", "turn_in_place"])
        self.assertEqual(pod.seen[0][1], {"serial": SERIAL, "priority": "default"})
        self.assertEqual(pod.seen[1][1], {"serial": SERIAL, "dist_mm": "100.0", "speed_mmps": "75.0"})
        self.assertEqual(pod.seen[2][1], {"serial": SERIAL, "angle_deg": "45.0", "speed_dps": "90.0"})
        self.assertEqual(slept, [1.5])
        self.assertEqual(mgr.cancelled, ["manual control"])
        self.assertTrue(ctl.hold.held)
        live = [t for t in hass.loop.timers if not t[2]]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0][0], 60.0)

    def test_refused_before_anything_is_sent(self) -> None:
        async def body(pod, base, session):
            out = []
            for setup in ("quiet", "cryo", "offline"):
                mgr = _Manager()
                mgr.quiet = setup == "quiet"
                mgr.cryosleep.enabled = setup == "cryo"
                ctl, _, _, _ = _control(session, base, mgr, reachable=setup != "offline")
                try:
                    await ctl.command(*MBUTTONS.step_params("forward"))
                    out.append("NO_ERROR")
                except HA_ERROR as exc:
                    out.append(str(exc))
                out.append(ctl.hold.held)
            return pod, out
        pod, out = asyncio.run(_with_pod(body))
        self.assertEqual(pod.seen, [])
        self.assertIn("Quiet mode", out[0])
        self.assertIn("Cryosleep", out[2])
        self.assertIn("not reachable", out[4])
        self.assertEqual(out[1::2], [False, False, False])

    def test_pod_refusal_and_failed_assume(self) -> None:
        async def body(pod, base, session):
            mgr = _Manager()
            ctl, _, _, _ = _control(session, base, mgr)
            pod.answers["/api-sdk/drive_straight"] = (200, "error: on_charger")
            errors = []
            try:
                await ctl.command(*MBUTTONS.step_params("forward"))
            except HA_ERROR as exc:
                errors.append(str(exc))
            held_after_refusal = ctl.hold.held
            ctl2, _, _, _ = _control(session, base, _Manager())
            pod.answers["/api-sdk/assume_behavior_control"] = (500, "no")
            try:
                await ctl2.command(*MBUTTONS.step_params("forward"))
            except HA_ERROR as exc:
                errors.append(str(exc))
            return pod, errors, held_after_refusal, ctl2.hold.held
        pod, errors, held1, held2 = asyncio.run(_with_pod(body))
        self.assertIn("charger", errors[0])
        self.assertTrue(held1)
        self.assertIn("wire-pod did not take", errors[1])
        self.assertFalse(held2)
        self.assertEqual(pod.paths()[-1], "assume_behavior_control")

    def test_stop_works_during_quiet_mode_and_release_is_sent_once(self) -> None:
        async def body(pod, base, session):
            mgr = _Manager()
            ctl, _, _, _ = _control(session, base, mgr)
            await ctl.command(None)
            mgr.quiet = True
            await ctl.stop()
            await ctl.release()
            await ctl.release()
            return pod, ctl
        pod, ctl = asyncio.run(_with_pod(body))
        self.assertEqual(pod.paths(), ["assume_behavior_control", "stop_motors", "release_behavior_control"])
        self.assertFalse(ctl.hold.held)

    def test_idle_timer_releases(self) -> None:
        async def body(pod, base, session):
            clock = [1000.0]
            ctl, hass, _, _ = _control(session, base, _Manager(), clock=lambda: clock[0])
            await ctl.command(*MBUTTONS.step_params("backward"))
            timer = [t for t in hass.loop.timers if not t[2]][-1]
            clock[0] = 1059.0
            timer[1]()                       # fires early: reschedules, does not release
            early = list(pod.paths())
            timer2 = [t for t in hass.loop.timers if not t[2]][-1]
            clock[0] = 1060.0
            timer2[1]()
            await asyncio.gather(*hass.tasks)
            return pod, early, ctl, timer2[0]
        pod, early, ctl, delay2 = asyncio.run(_with_pod(body))
        self.assertEqual(early, ["assume_behavior_control", "drive_straight"])
        self.assertAlmostEqual(delay2, 1.0)
        self.assertEqual(pod.paths()[-1], "release_behavior_control")
        self.assertFalse(ctl.hold.held)

    def test_cryosleep_lets_go(self) -> None:
        async def body(pod, base, session):
            mgr = _Manager()
            ctl, hass, _, _ = _control(session, base, mgr)
            await ctl.command(None)
            mgr.cryosleep.enabled = True
            for cb in list(mgr.listeners):
                cb()
            await asyncio.gather(*hass.tasks)
            before_shutdown = (list(pod.paths()), ctl.hold.held)
            await ctl.shutdown()
            return pod, before_shutdown, mgr
        pod, before_shutdown, mgr = asyncio.run(_with_pod(body))
        self.assertEqual(before_shutdown, (["assume_behavior_control", "release_behavior_control"], False))
        self.assertEqual(pod.paths(), ["assume_behavior_control", "release_behavior_control"])
        self.assertEqual(mgr.listeners, [])

    def test_eye_colour_needs_no_control(self) -> None:
        async def body(pod, base, session):
            mgr = _Manager()
            mgr.quiet = True
            ctl, _, _, _ = _control(session, base, mgr)
            await ctl.setting(*SELECT.eye_command("Lime", None))
            mgr.cryosleep.enabled = True
            try:
                await ctl.setting(*SELECT.eye_command("Teal", None))
                err = None
            except HA_ERROR as exc:
                err = str(exc)
            return pod, err
        pod, err = asyncio.run(_with_pod(body))
        self.assertEqual(pod.seen, [("/api-sdk/eye_color", {"serial": SERIAL, "color": "3"})])
        self.assertIn("Cryosleep", err)


class ExtrasReadTests(unittest.TestCase):
    def test_parts_fail_on_their_own(self) -> None:
        import json

        async def body(pod, base, session):
            pod.answers["/api-sdk/get_robot_stats"] = (200, json.dumps(DEMO_STATS))
            pod.answers["/api-sdk/get_sdk_settings"] = (200, "error: bot refuses to return robotsettings")
            pod.answers["/api-sdk/get_sdk_info"] = (200, json.dumps({"global_guid": "g", "robots": [
                {"esn": "0DD1CAFE", "ip_address": "10.0.0.5", "guid": "secret", "activated": True}]}))
            one = await MANUAL.read_extras(session, base, SERIAL)
            for path in ("/api-sdk/get_robot_stats", "/api-sdk/get_sdk_info"):
                pod.answers[path] = (500, "down")
            try:
                await MANUAL.read_extras(session, base, SERIAL)
                failed = None
            except Exception as exc:  # UpdateFailed (stand-in or real)
                failed = type(exc).__name__
            return one, failed
        one, failed = asyncio.run(_with_pod(body))
        self.assertEqual(one["stats"]["distance_m"], 2120.88)
        self.assertIsNone(one["settings"])
        self.assertEqual(one["ip_address"], "10.0.0.5")
        self.assertNotIn("secret", repr(one))
        self.assertEqual(len(one["errors"]), 1)
        self.assertEqual(failed, "UpdateFailed")


class _Entry:
    def __init__(self, runtime) -> None:
        self.title = "Vector DEMO"
        self.data = {"serial": SERIAL, "wirepod_url": POD}
        self.runtime_data = runtime


class EntityTests(unittest.TestCase):
    def _entry(self, coord_data=None, extras_data=None):
        runtime = types.SimpleNamespace(session=None, base_url=POD, serial=SERIAL, manager=_Manager(),
                                        coordinator=types.SimpleNamespace(data=coord_data),
                                        hub=types.SimpleNamespace(frame_size=lambda: None),
                                        manual=None, extras=types.SimpleNamespace(data=extras_data))
        return _Entry(runtime)

    def test_manual_entities(self) -> None:
        entry = self._entry()
        hass = _FakeHass()
        buttons = MBUTTONS.manual_buttons(hass, entry)
        numbers = MNUMBERS.manual_numbers(hass, entry)
        switches = MSWITCH.manual_switches(hass, entry)
        self.assertIs(entry.runtime_data.manual, buttons[0]._control)
        self.assertEqual([b._attr_unique_id for b in buttons],
                         [f"{SERIAL}_manual_{k}" for k in ("forward", "backward", "turn_left", "turn_right", "stop")])
        self.assertEqual([(n._attr_native_min_value, n._attr_native_max_value) for n in numbers],
                         [(-22.0, 45.0), (0.0, 100.0)])
        self.assertEqual(switches[0]._attr_unique_id, f"{SERIAL}_manual_control")
        self.assertFalse(switches[0].is_on)
        entry.runtime_data.manager.cryosleep.enabled = True
        self.assertFalse(buttons[0].available)
        self.assertTrue(buttons[4].available)       # Stop stays available
        self.assertFalse(numbers[0].available)

    def test_step_params_match_controls(self) -> None:
        self.assertEqual(MBUTTONS.step_params("backward"),
                         (CONTROLS.PATH_DRIVE, {"dist_mm": -50.0, "speed_mmps": 50.0}))
        self.assertEqual(MBUTTONS.step_params("turn_right"),
                         (CONTROLS.PATH_TURN, {"angle_deg": -45.0, "speed_dps": 90.0}))
        for key in CONTROLS.STEPS:
            path, params = MBUTTONS.step_params(key)
            CONTROLS.control_url(POD, path, SERIAL, params)   # every step is inside the limits

    def test_eye_command(self) -> None:
        self.assertEqual(SELECT.eye_command("Teal", None), (CONTROLS.PATH_EYE, {"color": 0}))
        self.assertEqual(SELECT.eye_command("Green", None), (CONTROLS.PATH_EYE, {"color": 6}))
        settings = ROBOTDATA.normalize_settings(DEMO_SETTINGS)
        path, params = SELECT.eye_command("Custom", settings)
        self.assertEqual(path, CONTROLS.PATH_EYE_CUSTOM)
        self.assertAlmostEqual(params["hue"], 0.797, places=3)
        for option, s in (("Custom", None), ("Custom", {"custom_hue": None}), ("Pink", None)):
            with self.assertRaises(ValueError):
                SELECT.eye_command(option, s)

    def test_data_sensors(self) -> None:
        extras = {"stats": ROBOTDATA.normalize_stats(DEMO_STATS),
                  "settings": ROBOTDATA.normalize_settings(DEMO_SETTINGS), "ip_address": "10.0.0.5"}
        entry = self._entry(coord_data={"robot_reachable": True, "robot_state": {"moving": True, "picked_up": False}},
                            extras_data=extras)
        sensors = {s._attr_unique_id.split("_", 1)[1]: s for s in EXTRAS.extra_sensors(_FakeHass(), entry)}
        self.assertEqual(sensors["distance_driven"].native_value, 2120.88)
        self.assertEqual([sensors[k]._attr_suggested_display_precision for k in ("time_on", "time_petted", "distance_driven")],
                         [1, 0, 1])
        self.assertEqual(sensors["time_on"].native_value, 51732922)
        self.assertEqual(sensors["time_petted"].native_value, 3216.9)
        self.assertEqual(sensors["wake_word_reactions"].native_value, 219)
        self.assertEqual(sensors["lifetime_counters"].native_value, 6)
        self.assertEqual(sensors["lifetime_counters"].extra_state_attributes["Odom.Body"], 212088151195.0)
        self.assertEqual(sensors["time_zone"].native_value, "Etc/UTC")
        self.assertEqual(sensors["locale"].extra_state_attributes["clock_24_hour"], True)
        self.assertEqual(sensors["ip_address"].native_value, "10.0.0.5")
        flags = {f._attr_unique_id.split("_", 1)[1]: f for f in EXTRAS.extra_binary_sensors(entry)}
        self.assertEqual(flags["moving"].is_on, True)
        self.assertEqual(flags["picked_up"].is_on, False)
        self.assertIsNone(flags["animating"].is_on)          # not in the reading: unknown, not False
        entry.runtime_data.coordinator.data = {"robot_reachable": False, "robot_state": {"moving": True}}
        self.assertIsNone(flags["moving"].is_on)

    def test_say_text_entity(self) -> None:
        async def body(pod, base, session):
            entry = self._entry(coord_data={"robot_reachable": True})
            entry.runtime_data.session, entry.runtime_data.base_url = session, base
            control = MANUAL.ensure_control(_FakeHass(), entry)
            control._sleep = lambda s: asyncio.sleep(0)
            say = TEXT.SayText(entry, control)
            say.async_write_ha_state = lambda: None     # not added to a running Home Assistant here
            await say.async_set_value("  hello there  ")
            first_state = (say.native_value, say.extra_state_attributes["last_said"])
            await say.async_set_value("hello there")
            await say.async_set_value("   ")
            await say.async_set_value("")
            calls_after_clear = list(pod.paths())
            # Quiet mode is lifted temporarily for Say, then released & restored even on error.
            mgr = entry.runtime_data.manager
            mgr.quiet = True
            await say.async_set_value("during quiet")
            quiet_after_ok = (mgr.quiet, list(mgr.quiet_calls), control.hold.held)
            pod.answers["/api-sdk/say_text"] = (500, "boom")
            try:
                await say.async_set_value("fail while quiet")
                quiet_err = None
            except HA_ERROR as exc:
                quiet_err = str(exc)
            quiet_after_err = (mgr.quiet, list(mgr.quiet_calls), control.hold.held, quiet_err)
            pod.answers.pop("/api-sdk/say_text", None)
            mgr.quiet = False
            mgr.cryosleep.enabled = True
            try:
                await say.async_set_value("blocked in cryo")
                cryo_err = None
            except HA_ERROR as exc:
                cryo_err = str(exc)
            mgr.cryosleep.enabled = False
            try:
                await say.async_set_value("bad\x07")
                bad_err = None
            except HA_ERROR as exc:
                bad_err = str(exc)
            return pod, first_state, calls_after_clear, quiet_after_ok, quiet_after_err, cryo_err, bad_err, say.extra_state_attributes
        pod, first_state, calls_after_clear, quiet_after_ok, quiet_after_err, cryo_err, bad_err, attrs = asyncio.run(_with_pod(body))
        self.assertEqual(first_state, ("", "hello there"))
        self.assertEqual(calls_after_clear, ["assume_behavior_control", "say_text", "say_text"])
        self.assertEqual(pod.seen[1][1]["text"], "hello there")
        self.assertEqual(pod.seen[2][1]["text"], "hello there")
        self.assertEqual(quiet_after_ok, (True, [False, True], False))
        self.assertEqual(quiet_after_err[:3], (True, [False, True, False, True], False))
        self.assertIn("wire-pod did not take", quiet_after_err[3])
        self.assertIn("Cryosleep", cryo_err)
        self.assertIn("control characters", bad_err)
        self.assertEqual(attrs["last_said"], "during quiet")

    def test_eye_select_tracks_cryosleep_displacement(self) -> None:
        async def body():
            extras = types.SimpleNamespace(data={"settings": ROBOTDATA.normalize_settings(DEMO_SETTINGS)},
                                           last_update_success=True,
                                           async_add_listener=lambda *a, **kw: (lambda: None))
            entry = self._entry(coord_data={"robot_reachable": True})
            control = MANUAL.ensure_control(_FakeHass(), entry)
            sel = SELECT.EyeColourSelect(entry, control, extras)
            writes, removers = [], []
            sel.async_write_ha_state = lambda: writes.append(sel.available)
            sel.async_on_remove = removers.append
            await sel.async_added_to_hass()
            self.assertGreaterEqual(len(removers), 1)
            self.assertTrue(sel.available)
            entry.runtime_data.manager.cryosleep.enabled = True
            for cb in list(entry.runtime_data.manager.listeners):
                cb()
            self.assertEqual(writes, [False])
            for rem in removers:
                rem()
            writes.clear()
            entry.runtime_data.manager.cryosleep.enabled = False
            for cb in list(entry.runtime_data.manager.listeners):
                cb()
            self.assertEqual(writes, [])
        asyncio.run(body())


if __name__ == "__main__":
    unittest.main()

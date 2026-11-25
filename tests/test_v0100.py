"""Tests for 0.10.0 Cryosleep mode: pod sequence, poll/lock, restart restore, and HA displacement."""
from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

POD = "http://pod:8080"


def _make_entity_base() -> type:
    class _EntityBase:
        async def async_added_to_hass(self) -> None:
            return None

        def async_on_remove(self, fn) -> None:
            return None

        def async_write_ha_state(self) -> None:
            return None

        def async_schedule_update_ha_state(self, force_refresh: bool = False) -> None:
            return None

    assert hasattr(_EntityBase, "async_added_to_hass")
    return _EntityBase


def _make_coord_base(entity_base: type) -> type:
    class _CoordinatorEntity(entity_base):
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return bool(self.coordinator.last_update_success)

        async def async_added_to_hass(self) -> None:
            return None

    assert hasattr(_CoordinatorEntity, "available")
    return _CoordinatorEntity


def _make_restore_base(entity_base: type) -> type:
    class _RestoreEntity(entity_base):
        async def async_get_last_state(self):
            return None

    assert hasattr(_RestoreEntity, "async_get_last_state")
    return _RestoreEntity


def _ha_stub_specs(eb: type, cb: type, rb: type) -> dict[str, dict]:
    assert eb is not None and cb is not None and rb is not None
    return {
        "homeassistant": {},
        "homeassistant.components": {},
        "homeassistant.components.button": {"ButtonEntity": type("ButtonEntity", (eb,), {})},
        "homeassistant.components.camera": {"Camera": type("Camera", (eb,), {}), "async_get_still_stream": lambda *a, **kw: None},
        "homeassistant.components.light": {
            "ATTR_BRIGHTNESS": "brightness",
            "ColorMode": type("ColorMode", (), {"BRIGHTNESS": "brightness"}),
            "LightEntity": type("LightEntity", (eb,), {}),
        },
        "homeassistant.components.number": {
            "NumberEntity": type("NumberEntity", (eb,), {}),
            "NumberMode": type("NumberMode", (), {"AUTO": "auto", "BOX": "box", "SLIDER": "slider"}),
        },
        "homeassistant.components.switch": {"SwitchEntity": type("SwitchEntity", (eb,), {})},
        "homeassistant.config_entries": {"ConfigEntry": type("ConfigEntry", (), {})},
        "homeassistant.core": {
            "HomeAssistant": type("HomeAssistant", (), {}),
            "callback": lambda fn: (setattr(fn, "_hass_callback", True) or fn),
        },
        "homeassistant.exceptions": {"HomeAssistantError": type("HomeAssistantError", (Exception,), {})},
        "homeassistant.helpers": {},
        "homeassistant.helpers.aiohttp_client": {"async_get_clientsession": lambda *a, **kw: None},
        "homeassistant.helpers.device_registry": {"DeviceInfo": dict},
        "homeassistant.helpers.dispatcher": {
            "async_dispatcher_send": lambda *a, **kw: None,
            "async_dispatcher_connect": lambda *a, **kw: (lambda: None),
        },
        "homeassistant.helpers.entity_platform": {"AddConfigEntryEntitiesCallback": type("AddCb", (), {})},
        "homeassistant.helpers.event": {},
        "homeassistant.helpers.restore_state": {"RestoreEntity": rb},
        "homeassistant.helpers.update_coordinator": {
            "CoordinatorEntity": cb, "DataUpdateCoordinator": type("DataUpdateCoordinator", (), {}),
            "UpdateFailed": type("UpdateFailed", (Exception,), {}),
        },
    }


def _upgrade_stub_class(old: type, val: type, eb: type) -> None:
    assert isinstance(old, type) and isinstance(val, type)
    for m_name in ("async_added_to_hass", "async_on_remove", "async_write_ha_state", "async_schedule_update_ha_state"):
        if not hasattr(old, m_name):
            setattr(old, m_name, getattr(eb, m_name))
    for attr in ("__init__", "available", "async_added_to_hass", "async_get_last_state"):
        if attr in val.__dict__:
            setattr(old, attr, val.__dict__[attr])


def _install_missing_ha_stubs() -> None:
    eb = _make_entity_base()
    cb = _make_coord_base(eb)
    specs = _ha_stub_specs(eb, cb, _make_restore_base(eb))
    assert "homeassistant.components.switch" in specs
    for name, attrs in specs.items():
        existing = sys.modules.get(name)
        if existing is None:
            existing = types.ModuleType(name)
            sys.modules[name] = existing
        elif getattr(existing, "__file__", None) is not None:
            continue
        for attr, val in attrs.items():
            old = getattr(existing, attr, None)
            if isinstance(old, type) and isinstance(val, type) and attr.endswith("Entity"):
                _upgrade_stub_class(old, val, eb)
            elif old is None:
                setattr(existing, attr, val)
    sys.modules["homeassistant.helpers"].device_registry = sys.modules["homeassistant.helpers.device_registry"]


def _load_modules():
    try:
        for req in ("button", "light", "number", "switch"):
            importlib.import_module(f"homeassistant.components.{req}")
    except ModuleNotFoundError:
        _install_missing_ha_stubs()
    mods = {
        k: importlib.import_module(f"custom_components.vector_wirepod.{k}")
        for k in ("wirepod", "cryosleep", "manager", "switch", "light", "number", "button")
    }
    exc_cls = importlib.import_module("homeassistant.exceptions").HomeAssistantError
    assert hasattr(mods["cryosleep"], "CryosleepState") and hasattr(mods["switch"], "CryosleepSwitch")
    return mods, exc_cls


MODS, HA_ERROR = _load_modules()
CRYO = MODS["cryosleep"]
WIREPOD = MODS["wirepod"]


class _ScriptedPod:
    def __init__(self, *, quiet: bool = False, volume: int = 4, pre_quiet_vol: int = 4,
                 backlight: int = 10, mic_muted: bool = False, pod_gated: bool = False,
                 robot_muted: bool = False, asleep: bool = False, ssh_ok: bool = True,
                 console_ok: bool = True, reachable: bool = True) -> None:
        self.quiet = quiet
        self.volume = volume
        self.pre_quiet_vol = pre_quiet_vol
        self.backlight = backlight
        self.mic_muted = mic_muted
        self.pod_gated = pod_gated
        self.robot_muted = robot_muted
        self.asleep = asleep
        self.ssh_ok = ssh_ok
        self.console_ok = console_ok
        self.reachable = reachable
        self.log: list[tuple[str, object]] = []
        self.clock = 100.0

    def _check_online(self) -> None:
        if not self.reachable:
            raise WIREPOD.WirePodError("robot offline")

    async def _get_quiet(self):
        self.log.append(("get_quiet", self.quiet))
        return {"enabled": self.quiet}

    async def _set_quiet(self, on: bool):
        self.log.append(("set_quiet", on))
        self.quiet = on
        self.volume = 0 if on else self.pre_quiet_vol

    async def _get_backlight(self):
        self._check_online()
        if not self.ssh_ok:
            raise WIREPOD.WirePodError("robot_ssh_failed")
        self.log.append(("get_backlight", self.backlight))
        return self.backlight

    async def _set_backlight(self, lvl: int):
        self._check_online()
        if not self.ssh_ok:
            raise WIREPOD.WirePodError("robot_ssh_failed")
        self.log.append(("set_backlight", lvl))
        self.backlight = lvl
        return lvl

    async def _get_cryo_mute(self):
        self._check_online()
        if not self.console_ok:
            raise WIREPOD.WirePodError("cryo_mute: http 502: robot_console_failed")
        ans = {"muted": self.pod_gated and self.robot_muted,
               "pod_gated": self.pod_gated, "robot_muted": self.robot_muted}
        self.log.append(("get_cryo_mute", ans["muted"]))
        return ans

    async def _set_cryo_mute(self, muted: bool):
        self._check_online()
        self.pod_gated = muted
        if not self.console_ok:
            raise WIREPOD.WirePodError("cryo_mute: http 502: robot_console_failed")
        self.robot_muted = muted
        ans = {"muted": self.pod_gated and self.robot_muted,
               "pod_gated": self.pod_gated, "robot_muted": self.robot_muted}
        self.log.append(("set_cryo_mute", muted))
        return ans

    async def _get_robot_state(self):
        self._check_online()
        self.log.append(("get_robot_state", self.asleep))
        return {"asleep": self.asleep}

    async def _send_sleep(self):
        self._check_online()
        self.log.append(("send_sleep", None))

    async def _sleep(self, sec: float):
        self.clock += sec
        self.log.append(("sleep", sec))

    def io(self) -> CRYO.CryosleepIO:
        assert isinstance(self.log, list) and self.clock >= 0
        return CRYO.CryosleepIO(
            get_quiet=self._get_quiet, set_quiet=self._set_quiet,
            get_backlight=self._get_backlight, set_backlight=self._set_backlight,
            get_cryo_mute=self._get_cryo_mute, set_cryo_mute=self._set_cryo_mute,
            get_robot_state=self._get_robot_state, send_sleep=self._send_sleep,
            sleep=self._sleep, now=lambda: self.clock, failures=(WIREPOD.WirePodError,),
        )


def _new_pod_app(pod: _ScriptedPod, seen_paths: list[tuple[str, str]] | None = None):
    assert pod is not None and isinstance(pod.log, list)
    state_json = {
        "ok": True, "on_charger": True, "charging": False, "picked_up": False,
        "being_held": False, "button_pressed": False, "moving": False,
        "animating": False, "cliff_detected": False,
    }

    def cryo_handler(r: web.Request) -> web.Response:
        if r.method == "POST":
            m = r.query["muted"] == "true"
            pod.pod_gated, pod.robot_muted = m, m
        return web.json_response({
            "ok": True, "muted": pod.pod_gated and pod.robot_muted,
            "pod_gated": pod.pod_gated, "robot_muted": pod.robot_muted,
        })

    routes = {
        "/api-sdk/quiet_mode": lambda r: web.json_response({"enabled": setattr(pod, "quiet", r.query.get("enable") == "true") or pod.quiet} if r.method == "POST" else {"enabled": pod.quiet}),
        "/api-sdk/get_sdk_settings": lambda r: web.json_response({"master_volume": pod.volume}),
        "/api-sdk/volume": lambda r: (setattr(pod, "volume", int(r.query["volume"])), web.Response(text="done"))[1],
        "/api-sdk/backlight": lambda r: web.json_response({"ok": True, "level": (setattr(pod, "backlight", int(r.query["level"])) or pod.backlight) if r.method == "POST" else pod.backlight}),
        "/api-sdk/mic_mute": lambda r: web.json_response({"ok": True, "muted": (setattr(pod, "mic_muted", r.query["muted"] == "true") or pod.mic_muted) if r.method == "POST" else pod.mic_muted, "restarting": False}),
        "/api-sdk/cryo_mute": cryo_handler,
        "/api-sdk/robot_state": lambda r: web.json_response({**state_json, "asleep": pod.asleep}),
        "/api-sdk/get_battery": lambda r: web.json_response({"battery_level": 3, "battery_volts": 4.1, "is_on_charger_platform": True}),
        "/api-sdk/cloud_intent": lambda r: (pod.log.append(("http_intent", r.query.get("intent"))), web.Response(text="done"))[1],
    }

    async def dispatch(req: web.Request) -> web.Response:
        if seen_paths is not None:
            seen_paths.append((req.method, req.path))
        return routes[req.path](req)

    app = web.Application()
    for path in routes:
        app.router.add_route("*", path, dispatch)
    return app


class _CoordStub:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True
        self.listeners = 0

    def async_add_listener(self, cb, context=None):
        self.listeners += 1
        return lambda: None

    def async_update_listeners(self) -> None:
        return None

    async def async_request_refresh(self) -> None:
        return None


class _HassStub:
    def __init__(self, config_dir: str) -> None:
        self.config = types.SimpleNamespace(config_dir=config_dir)
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name: str = "") -> asyncio.Task:
        t = asyncio.create_task(coro, name=name)
        self.tasks.append(t)
        return t


class CryosleepCoreTests(unittest.TestCase):
    def test_enter_displaces_quiet_first_and_mutes_without_touching_volume(self) -> None:
        pod = _ScriptedPod(quiet=True, volume=0, pre_quiet_vol=4, backlight=15, asleep=False)
        st = CRYO.CryosleepState()
        st.arm(camera_allowed=True)
        asyncio.run(CRYO.execute_enter(st, pod.io(), lambda: False, quiet_settle_s=0.1))
        self.assertTrue(st.applied)
        self.assertTrue(st.cryo_muted)
        self.assertEqual(st.phase, CRYO.PHASE_ACTIVE)
        self.assertEqual(st.saved_backlight, 15)
        names = [k for k, _ in pod.log]
        self.assertLess(names.index("set_quiet"), names.index("set_cryo_mute"))
        self.assertEqual((pod.volume, pod.backlight, pod.pod_gated, pod.robot_muted), (4, 0, True, True))
        self.assertNotIn(("send_sleep", None), pod.log)

    def test_non_ssh_and_non_console_robot_still_sleeps_and_gates_pod(self) -> None:
        pod = _ScriptedPod(volume=5, ssh_ok=False, console_ok=False, asleep=False)
        st = CRYO.CryosleepState()
        st.arm(camera_allowed=True)
        asyncio.run(CRYO.execute_enter(st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertTrue(st.applied)
        self.assertIsNone(st.saved_backlight)
        self.assertTrue(pod.pod_gated)
        self.assertIn(("send_sleep", None), pod.log)

    def test_offline_arming_then_online_apply_and_fallback_lock(self) -> None:
        pod = _ScriptedPod(reachable=False, volume=2, backlight=8, asleep=False)
        st = CRYO.CryosleepState()
        st.arm(camera_allowed=False)
        asyncio.run(CRYO.execute_enter(st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertFalse(st.applied)
        self.assertEqual(st.phase, CRYO.PHASE_WAITING_ROBOT)
        self.assertEqual(st.decide_poll(False, None, 110.0), CRYO.POLL_NOOP)
        self.assertEqual(st.decide_poll(True, False, 120.0), CRYO.POLL_APPLY)
        pod.reachable = True
        asyncio.run(CRYO.execute_poll_action(CRYO.POLL_APPLY, st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertTrue(st.applied)
        self.assertEqual(st.saved_camera_allowed, False)
        pod.asleep = True
        self.assertEqual(st.decide_poll(True, True, 140.0), CRYO.POLL_SETTLED_ASLEEP)
        self.assertEqual(st.decide_poll(True, False, 170.0), CRYO.POLL_NOOP)
        self.assertEqual(st.decide_poll(True, False, 200.0), CRYO.POLL_REENFORCE_SLEEP)
        self.assertEqual(st.decide_poll(True, False, 270.0), CRYO.POLL_REENFORCE_SLEEP)
        self.assertEqual(st.decide_poll(True, False, 340.0), CRYO.POLL_LOCK_STILL)
        asyncio.run(CRYO.execute_poll_action(CRYO.POLL_LOCK_STILL, st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertTrue(pod.quiet)
        self.assertTrue(pod.pod_gated and pod.robot_muted)
        self.assertEqual((st.phase, st.enforcements), (CRYO.PHASE_HOLDING_STILL, 3))

    def test_restart_preserve_snapshot_and_morning_exit_restore(self) -> None:
        pod = _ScriptedPod(quiet=True, volume=4, backlight=0, pod_gated=True, robot_muted=True, asleep=True)
        st = CRYO.CryosleepState()
        st.restore_from_attrs({
            "saved_backlight": 0, "saved_camera_allowed": True, "cryo_muted": True,
            "snapshot_taken": True, "holding_reserve": True, "enforcements": 2,
        }, fallback_camera_allowed=False)
        st.record_snapshot(19)
        asyncio.run(CRYO.execute_enter(st, pod.io(), lambda: False, quiet_settle_s=0.0))
        self.assertEqual((st.saved_backlight, st.saved_camera_allowed), (0, True))
        pod.asleep = False
        st.holding_reserve = True
        st.begin_exit()
        cam = asyncio.run(CRYO.execute_exit(st, pod.io(), lambda: False))
        self.assertIs(cam, True)
        self.assertFalse(pod.quiet)
        self.assertFalse(pod.pod_gated or pod.robot_muted)
        self.assertEqual((pod.volume, pod.backlight), (4, 10))
        self.assertEqual(st.phase, CRYO.PHASE_OFF)
        st2 = CRYO.CryosleepState()
        st2.record_snapshot(0)
        self.assertEqual(st2.target_restore_backlight(True), 0)
        self.assertEqual(st2.target_restore_backlight(False), 10)


class CryosleepHAIntegrationTests(unittest.TestCase):
    def _setup_entities(self, pod: _ScriptedPod, session: aiohttp.ClientSession, base: str, tmpdir: str):
        hass = _HassStub(tmpdir)
        hub = WIREPOD.CameraHub(session, WIREPOD.HubConfig(base_url=base, serial="0dd1cafe"))
        entry = types.SimpleNamespace(
            title="Vector DEMO",
            data={"serial": "0dd1cafe", "wirepod_url": base},
            options={},
            runtime_data=None,
        )
        mgr = MODS["manager"].VectorManager(hass, entry, session, hub)
        coord = _CoordStub({"robot_reachable": True, "quiet_mode": {"enabled": False}})
        mgr.coordinator = coord
        entry.runtime_data = types.SimpleNamespace(
            hub=hub, coordinator=coord, session=session, base_url=base, serial="0dd1cafe", manager=mgr,
        )
        return hass, mgr, {
            "cryo": MODS["switch"].CryosleepSwitch(entry),
            "cam": MODS["switch"].CameraAllowedSwitch(entry),
            "quiet": MODS["switch"].QuietModeSwitch(entry),
            "wake_photo": MODS["switch"].PhotoOnWakeSwitch(entry),
            "mic": MODS["switch"].MicMuteSwitch(entry),
            "bl": MODS["light"].FaceBacklight(entry),
            "vol": MODS["number"].VectorVolumeNumber(entry),
            "dock": MODS["button"].DockButton(entry),
        }

    async def _wire_and_run_cycle(self, ents: dict, hass: _HassStub) -> dict:
        assert "cryo" in ents and hass is not None
        refreshes, writes = [], []
        for name, ent in ents.items():
            ent.async_on_remove = lambda _fn: None
            ent.async_write_ha_state = lambda n=name: writes.append(n)
            ent.async_schedule_update_ha_state = lambda force_refresh=False, n=name: refreshes.append((n, force_refresh))
            ent.async_get_last_state = _AsyncNone
            await ent.async_added_to_hass()
        for key in ("mic", "bl", "vol"):
            await ents[key].async_update()
        avail_before = all(ents[k].available for k in ("cam", "quiet", "wake_photo", "mic", "bl", "vol", "dock"))
        await ents["cryo"].async_turn_on()
        await asyncio.gather(*hass.tasks)
        hass.tasks.clear()
        avail_during = {k: ents[k].available for k in ("cam", "quiet", "wake_photo", "mic", "bl", "vol", "dock")}
        blocked = await self._probe_blocked_calls(ents)
        await ents["cryo"].async_turn_off()
        await asyncio.gather(*hass.tasks)
        avail_after = all(ents[k].available for k in ("cam", "quiet", "wake_photo", "mic", "bl", "vol", "dock"))
        return {"before": avail_before, "during": avail_during, "blocked": blocked,
                "after": avail_after, "refreshes": refreshes}

    def test_cryosleep_displaces_and_restores_all_ha_controls(self) -> None:
        pod = _ScriptedPod(quiet=False, volume=4, backlight=10, mic_muted=False, asleep=True)

        async def run() -> dict:
            runner = web.AppRunner(_new_pod_app(pod))
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            base = f"http://127.0.0.1:{runner.addresses[0][1]}"
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    async with aiohttp.ClientSession() as session:
                        hass, mgr, ents = self._setup_entities(pod, session, base, tmp)
                        out = await self._wire_and_run_cycle(ents, hass)
                        out["hub_allowed"] = mgr.hub.allowed
                        out["vol"] = pod.volume
                        out["cryo_muted"] = (pod.pod_gated, pod.robot_muted)
                        return out
                finally:
                    await runner.cleanup()

        out = asyncio.run(run())
        self.assertTrue(out["before"])
        self.assertTrue(all(not v for v in out["during"].values()))
        self.assertEqual(out["blocked"], ["quiet", "bl", "vol", "dock", "cam", "mic", "wake_photo"])
        self.assertTrue(out["after"])
        self.assertTrue(out["hub_allowed"])
        self.assertEqual(out["vol"], 4)
        self.assertEqual(out["cryo_muted"], (False, False))
        self.assertEqual(sorted(out["refreshes"]), [("bl", True), ("mic", True), ("vol", True)])

    async def _probe_blocked_calls(self, ents: dict) -> list[str]:
        blocked = []
        checks = [
            ("quiet", ents["quiet"].async_turn_on()),
            ("bl", ents["bl"].async_turn_on(brightness=128)),
            ("vol", ents["vol"].async_set_native_value(3.0)),
            ("dock", ents["dock"].async_press()),
            ("cam", ents["cam"].async_turn_on()),
            ("mic", ents["mic"].async_turn_on()),
            ("wake_photo", ents["wake_photo"].async_turn_on()),
        ]
        for label, coro in checks:
            try:
                await coro
            except HA_ERROR:
                blocked.append(label)
        assert len(checks) == 7
        return blocked


async def _AsyncNone():
    return None


if __name__ == "__main__":
    unittest.main()

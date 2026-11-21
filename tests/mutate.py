"""Break one thing at a time and confirm the tests notice.
Each mutation must apply exactly once and change the file; a comment-only change must pass."""
import pathlib, shutil, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent.parent  # repository root
MUTATIONS = [
    ("control", "sleepcycle.py", "ACT_PHOTO = \"photo\"", "ACT_PHOTO = \"photo\"  # neutral", False),
    ("wake does not start quiet", "sleepcycle.py", "return ([ACT_PHOTO] if self.photo_on_wake else []) + [ACT_QUIET_ON]", "return ([ACT_PHOTO] if self.photo_on_wake else [])", True),
    ("quiet timer never returns to sleep", "sleepcycle.py", "if now - self.since >= self.quiet_hold_s:", "if False:", True),
    ("unknown reading treated as awake", "sleepcycle.py", "if asleep is None or self.phase == IDLE:", "if self.phase == IDLE:", True),
    ("sleep asked before quiet is lifted", "sleepcycle.py", "self._enter(GOING, now, \"sleep requested\")\n        return [ACT_QUIET_OFF, ACT_SLEEP]", "self._enter(GOING, now, \"sleep requested\")\n        return [ACT_SLEEP, ACT_QUIET_OFF]", True),
    ("photo after quiet mode", "sleepcycle.py", "return ([ACT_PHOTO] if self.photo_on_wake else []) + [ACT_QUIET_ON]", "return [ACT_QUIET_ON] + ([ACT_PHOTO] if self.photo_on_wake else [])", True),
    ("sleep request never gives up", "sleepcycle.py", "elif now - self.since > self.settle_s:", "elif False:", True),
    ("photos never pruned", "photos.py", "for old in list_photos(directory)[:-keep]:", "for old in []:", True),
    ("non-JPEG accepted", "photos.py", "if not is_jpeg(frame):", "if False:", True),
    ("status image forced to 16:9", "placeholder.py", "img = src.convert(\"RGB\").resize(canvas_size(src.size))", "img = src.convert(\"RGB\").resize((WIDTH, DEFAULT_HEIGHT))", True),
    ("remembered shape ignored", "placeholder.py", "img = Image.new(\"RGB\", canvas_size(size), (16, 16, 20))", "img = Image.new(\"RGB\", canvas_size(None), (16, 16, 20))", True),
    ("width and height swapped", "wirepod.py", "return (width, height) if width > 0 and height > 0 else None", "return (height, width) if width > 0 and height > 0 else None", True),
    ("missing endpoint is an error", "wirepod.py", "        if resp.status == 404:\n            return None\n        if resp.status != 200:\n            raise WirePodError(f\"robot_state:", "        if resp.status != 200:\n            raise WirePodError(f\"robot_state:", True),
    ("robot flag not checked", "wirepod.py", "if not isinstance(data.get(flag), bool):", "if False:", True),
    ("camera opened on a sleeping robot", "wirepod.py", "return (self._allowed and self._reachable and not self._asleep", "return (self._allowed and self._reachable", True),
    ("asleep state hides a withheld camera", "wirepod.py", "            if self._allowed and self._reachable:\n                self._set_state(STATE_ASLEEP,", "            if True:\n                self._set_state(STATE_ASLEEP,", True),
    ("serial guess beats the frame", "hardware.py", "        return GEN_1 if frame_size[0] / frame_size[1] > WIDE_RATIO else GEN_2\n", "        pass\n", True),
    ("every robot shown as 2.0", "hardware.py", "    if generation(serial, frame_size) == GEN_2:", "    if True:", True),
    ("backlight level not range-checked", "wirepod.py", "or not isinstance(level, int) or not 0 <= level <= BACKLIGHT_MAX):", "or not isinstance(level, int)):", True),
    ("no backlight login reported as an error", "wirepod.py", "        if resp.status in (404, 503):\n            return None\n        if resp.status != 200:\n            raise WirePodError(f\"backlight:", "        if resp.status != 200:\n            raise WirePodError(f\"backlight:", True),
    ("wake photo asked for only once", "photos.py", "    for n in range(tries):", "    for n in range(1):", True),
    ("keeps asking after a frame came", "photos.py", "        if frame is not None:\n            return frame\n        if n + 1 < tries:", "        if n + 1 < tries:", True),
    ("no pause between tries", "photos.py", "            await sleep(pause_s)\n    return None", "            pass\n    return None", True),
    ("pause after the last try", "photos.py", "        if n + 1 < tries:\n            await sleep(pause_s)", "        if True:\n            await sleep(pause_s)", True),
    ("any number of tries accepted", "photos.py", "    assert 1 <= tries <= MAX_FRAME_TRIES, ", "    assert True, ", True),
    ("retry_now keeps the wait", "wirepod.py", "        self._fails = 0\n        self._retry_at = 0.0\n        if self._state == STATE_NO_FRAMES:", "        if self._state == STATE_NO_FRAMES:", True),
    ("muted accepted on any path", "wirepod.py", "if path not in (\"/api-sdk/mic_mute\", \"/api-sdk/cryo_mute\") or serial is None or not isinstance(muted, bool):", "if serial is None or not isinstance(muted, bool):", True),
    ("muted sent inverted", "wirepod.py", "url += \"&muted=\" + (\"true\" if muted else \"false\")", "url += \"&muted=\" + (\"false\" if muted else \"true\")", True),
    ("no mute login reported as an error", "wirepod.py", "        if resp.status == 404 or (accept_503 and resp.status == 503):\n            return None", "        if False:\n            return None", True),
    ("pod's mute error code dropped", "wirepod.py", "raise WirePodError(f\"{label}: http {resp.status}: {code or text.strip()[:120]}\")", "raise WirePodError(f\"{label}: http {resp.status}\")", True),
    ("muted not checked to be a bool", "wirepod.py", "if not isinstance(data.get(\"muted\"), bool) or not isinstance(data.get(\"restarting\"), bool):", "if not isinstance(data.get(\"restarting\"), bool):", True),
    ("restarting not required", "wirepod.py", "if not isinstance(data.get(\"muted\"), bool) or not isinstance(data.get(\"restarting\"), bool):", "if not isinstance(data.get(\"muted\"), bool):", True),
    ("explore intent wrong string", "wirepod.py", "INTENT_EXPLORE = \"explore_start\"", "INTENT_EXPLORE = \"intent_explore_start\"", True),
    ("trigger_wake_word allowed without serial", "wirepod.py", "    if path == \"/api-sdk/trigger_wake_word\" and serial is None:", "    if False:", True),
    ("trigger_wake_word ignores body", "wirepod.py", "        if resp.status != 200 or text != \"success\":\n            raise WirePodError(f\"trigger_wake_word:", "        if resp.status != 200:\n            raise WirePodError(f\"trigger_wake_word:", True),
    ("stimulate button skips cycle cancel", "button.py", "    manager.cancel_cycle(f\"{label.lower()} requested\")", "    pass", True),
    ("stimulate button skips lifting quiet mode", "button.py", "        if quiet_was_on:\n            await manager.set_quiet(False)", "        if False:\n            await manager.set_quiet(False)", True),
    ("stimulate button fails to restore quiet on error", "button.py", "        if quiet_was_on:\n            await manager.set_quiet(True)", "        if False:\n            await manager.set_quiet(True)", True),
    ("setup omits stimulate buttons", "button.py", "        *[StimulateIntentButton(entry, *spec) for spec in STIMULATE_BUTTON_SPECS],", "        *[],", True),
    ("volume not range-checked", "wirepod.py", "or not isinstance(volume, int) or not VOLUME_MIN <= volume <= VOLUME_MAX):", "or not isinstance(volume, int)):", True),
    ("get_volume accepts error text", "wirepod.py", "    if stripped.startswith(\"error\"):", "    if False:", True),
    ("get_volume accepts out-of-range value", "wirepod.py", "or not VOLUME_MIN <= vol <= VOLUME_MAX:", "or False:", True),
    ("set_volume ignores body", "wirepod.py", "        if resp.status != 200 or text != \"done\":\n            raise WirePodError(f\"volume:", "        if resp.status != 200:\n            raise WirePodError(f\"volume:", True),
    ("volume polls an unreachable robot", "number.py", "        if isinstance(coord_data, dict) and coord_data.get(\"robot_reachable\") is False:", "        if False:", True),
    ("volume set skips state write", "number.py", "        self._level = target\n        self.async_write_ha_state()", "        self._level = target", True),
    ("cryosleep snapshots before displacing quiet", "cryosleep.py", "    await _displace_quiet(state, io, quiet_settle_s)\n    if cancelled():", "    if cancelled():", True),
    ("cryosleep overwrites snapshot on restart", "cryosleep.py", "        if self.snapshot_taken:\n            return", "        if False:\n            return", True),
    ("cryosleep skips cryo_mute on enter", "cryosleep.py", "    merr = await _apply_cryo_mute(state, io)\n    if cancelled():", "    merr = None\n    if cancelled():", True),
    ("cryosleep skips cryo_mute release on exit", "cryosleep.py", "    await _try_call(io, lambda: io.set_cryo_mute(False))", "    pass", True),
    ("cryosleep drift guard skips re-muting", "cryosleep.py", "    if isinstance(cm, dict) and not cm.get(\"muted\"):\n        cm, _ = await _try_call(io, lambda: io.set_cryo_mute(True))", "    if False:\n        cm, _ = await _try_call(io, lambda: io.set_cryo_mute(True))", True),
    ("cryosleep skips re-asserting cryo_mute after quiet unlock", "cryosleep.py", "            state.holding_reserve = False\n            await _apply_cryo_mute(state, io)", "            state.holding_reserve = False", True),
    ("cryo_mute accepts bad pod_gated type", "wirepod.py", "    if (not isinstance(data.get(\"muted\"), bool) or not isinstance(data.get(\"pod_gated\"), bool)\n            or not isinstance(data.get(\"robot_muted\"), bool)):", "    if not isinstance(data.get(\"muted\"), bool):", True),
    ("cryosleep never locks tracks when sleep fails", "cryosleep.py", "            if not self.holding_reserve:\n                self.holding_reserve, self.phase = True, PHASE_HOLDING_STILL", "            if False:\n                self.holding_reserve, self.phase = True, PHASE_HOLDING_STILL", True),
    ("morning wake exit leaves backlight off", "cryosleep.py", "        if self.saved_backlight is not None and self.saved_backlight > 0:\n            return self.saved_backlight\n        return DEFAULT_AWAKE_BACKLIGHT", "        if self.saved_backlight is not None:\n            return self.saved_backlight\n        return DEFAULT_AWAKE_BACKLIGHT", True),
    ("quiet mode stays available during cryosleep", "switch.py", "        if cryosleep_displaced(self._data.manager) or not super().available:", "        if not super().available:", True),
    ("face backlight ignores cryosleep guard", "light.py", "        guard_not_cryosleep(getattr(data, \"manager\", None), HomeAssistantError)", "        pass", True),
    ("volume slider ignores cryosleep guard", "number.py", "        guard_not_cryosleep(getattr(data, \"manager\", None), HomeAssistantError)", "        pass", True),
    ("buttons stay available during cryosleep", "button.py", "    @property\n    def available(self) -> bool:\n        return not cryosleep_displaced(self._manager)", "    @property\n    def available(self) -> bool:\n        return True", True),
    ("exiting cryosleep keeps controls blocked", "cryosleep.py", "return bool(cryo is not None and cryo.enabled)", "return bool(cryo is not None and (cryo.enabled or cryo.phase == PHASE_EXITING))", True),
    ("stop_cryosleep delays restoring camera", "manager.py", "        self.cryosleep.begin_exit()\n        self.hub.set_allowed(restore_cam)", "        self.cryosleep.begin_exit()\n        _ = restore_cam", True),
    ("cryosleep wakes robot with send_sleep when force sleep works", "cryosleep.py", "    if not state.cryo_muted and asleep is False:\n        _, serr = await _try_call(io, io.send_sleep)", "    if asleep is False:\n        _, serr = await _try_call(io, io.send_sleep)", True),
    ("cryosleep sends send_sleep on robot_state timeout", "cryosleep.py", "    if not state.cryo_muted and asleep is False:", "    if not state.cryo_muted:", True),
    ("poll wakes robot on single asleep flicker", "cryosleep.py", "            self.awake_polls += 1\n            if self.awake_polls < 2:\n                return POLL_NOOP", "            self.awake_polls += 1\n            if False:\n                return POLL_NOOP", True),
    ("cryosleep exit overwrites manual backlight", "cryosleep.py", "    if state.snapshot_taken and not (callable(skip_backlight) and skip_backlight()):", "    if state.snapshot_taken:", True),
    ('control 0.12.0', 'controls.py', 'PRIORITY = "default"', 'PRIORITY = "default"  # neutral', False),
    ('assume asks for override', 'controls.py', 'return url + "&priority=" + PRIORITY', 'return url + "&priority=override_behaviors"', True),
    ('priority constant changed', 'controls.py', 'PRIORITY = "default"', 'PRIORITY = "high"', True),
    ('control ranges not checked', 'controls.py', '    if not low <= float(value) <= high:', '    if False:', True),
    ('bools taken as numbers', 'controls.py', '    if isinstance(value, bool) or not isinstance(value, (int, float)):\n        raise ValueError(f"{name} must be a number', '    if not isinstance(value, (int, float)):\n        raise ValueError(f"{name} must be a number', True),
    ('fractional eye colour accepted', 'controls.py', '        if float(value) != int(value):', '        if False:', True),
    ('control characters spoken', 'controls.py', '    if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean):', '    if False:', True),
    ('speech length unchecked', 'controls.py', '    if not 0 < len(clean) <= SAY_MAX_CHARS:', '    if not 0 < len(clean):', True),
    ('missing or extra arguments accepted', 'controls.py', '    if set(given) != {name for name, _, _ in specs}:', '    if False:', True),
    ('pod refusal taken as success', 'controls.py', '    if body.lower().startswith("error"):\n        raise ControlRefused', '    if False:\n        raise ControlRefused', True),
    ('http failure reported as a refusal', 'controls.py', '        raise WirePodError(f"{label}: http {status}: {text.strip()[:120]}")', '        raise ControlRefused(f"{label}: http {status}: {text.strip()[:120]}")', True),
    ('quiet mode does not stop controls', 'controls.py', '    if quiet_on:\n        return "Quiet mode', '    if False:\n        return "Quiet mode', True),
    ('cryosleep does not stop controls', 'controls.py', '    if cryosleep_on:\n        return "Cryosleep', '    if False:\n        return "Cryosleep', True),
    ('control never let go', 'controls.py', '        return due is not None and now >= due', '        return False', True),
    ('control asked for on every command', 'controls.py', '        fresh = not self.held', '        fresh = True', True),
    ('turn left goes right', 'controls.py', '    "turn_left": (PATH_TURN, 45.0, 90.0),', '    "turn_left": (PATH_TURN, -45.0, 90.0),', True),
    ('sleep cycle keeps running', 'manual.py', '        self.manager.cancel_cycle("manual control")', '        pass', True),
    ('no wait for the grant', 'manual.py', '        await self._sleep(self._grant_wait_s)', '        await self._sleep(0.0)', True),
    ('failed assume leaves control held', 'manual.py', '            self.hold.end()\n            raise', '            raise', True),
    ('refusals not checked', 'manual.py', '            if why is not None:\n                raise HomeAssistantError(why)', '            if False:\n                raise HomeAssistantError(why)', True),
    ('cryosleep does not let go', 'manual.py', '        if self.displaced() and self.hold.held:', '        if False:', True),
    ('stop blocked by quiet mode', 'manual.py', '            if not self._reachable():\n                raise HomeAssistantError("Vector is not reachable through wire-pod")\n            if self.hold.held:', '            if self.why_not() is not None:\n                raise HomeAssistantError("Vector is not reachable through wire-pod")\n            if self.hold.held:', True),
    ('eyes changed during cryosleep', 'manual.py', '            if self.displaced():\n                raise HomeAssistantError("Cryosleep is on; turn it off first")', '            if False:\n                raise HomeAssistantError("Cryosleep is on; turn it off first")', True),
    ('release sent when not held', 'manual.py', '            if self.hold.end():\n                try:', '            self.hold.end()\n            if True:\n                try:', True),
    ('early idle timer not rescheduled', 'manual.py', '        else:\n            self._schedule_release()', '        else:\n            pass', True),
    ('odometer fixed point ignored', 'robotdata.py', 'round(odom / ODOM_FIXED_POINT / 1000.0, 2)', 'round(odom / 1000.0, 2)', True),
    ('petting left in milliseconds', 'robotdata.py', 'round(pet / 1000.0, 1)', 'round(pet, 1)', True),
    ('bool counted as a number', 'robotdata.py', '    if isinstance(value, bool) or not isinstance(value, (int, float)):\n        return None', '    if not isinstance(value, (int, float)):\n        return None', True),
    ('custom eye colour ignored', 'robotdata.py', '        custom_on = custom.get("enabled") is True and hue is not None and sat is not None', '        custom_on = False', True),
    ('eye preset index unchecked', 'robotdata.py', '0 <= preset < len(EYE_PRESETS)', '0 <= preset', True),
    ('stat keys unbounded', 'robotdata.py', '    for key in list(raw)[:MAX_STAT_KEYS]:', '    for key in list(raw):', True),
    ('unknown flag shown as off', 'extras.py', '        return value if isinstance(value, bool) else None', '        return bool(value)', True),
    ('flags shown while unreachable', 'extras.py', '        if not data.get("robot_reachable") or not isinstance(state, dict):', '        if not isinstance(state, dict):', True),
    ('eye preset off by one', 'select.py', '{"color": EYE_PRESETS.index(option)}', '{"color": EYE_PRESETS.index(option) + 1}', True),
    ('stop unavailable in cryosleep', 'manual_buttons.py', '    @property\n    def available(self) -> bool:\n        return True', '    @property\n    def available(self) -> bool:\n        return not self._control.displaced()', True),
]


def run_tests(root: pathlib.Path) -> bool:
    proc = subprocess.run([sys.executable, "-m", "unittest", "test_sleep_photos", "test_v050", "test_v060", "test_v061", "test_v070", "test_v080", "test_v090", "test_v0100", "test_v0110", "test_v0111", "test_v0120"],
                          cwd=root / "tests", capture_output=True, text=True, timeout=300)
    return proc.returncode == 0


def main() -> int:
    caught = survived = setup_errors = 0
    for name, fname, old, new, must_fail in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "t"
            shutil.copytree(HERE, root, ignore=shutil.ignore_patterns("__pycache__", ".git"))
            target = root / "custom_components" / "vector_wirepod" / fname
            text = target.read_text()
            if text.count(old) != 1 or old == new:
                print(f"SETUP-ERROR  {name}: needle found {text.count(old)} times"); setup_errors += 1; continue
            target.write_text(text.replace(old, new))
            if target.read_text() == text:
                print(f"SETUP-ERROR  {name}: file unchanged"); setup_errors += 1; continue
            passed = run_tests(root)
        if must_fail and not passed:
            caught += 1; print(f"CAUGHT       {name}")
        elif not must_fail and passed:
            print(f"CONTROL-OK   {name}")
        elif must_fail:
            survived += 1; print(f"SURVIVED     {name}")
        else:
            setup_errors += 1; print(f"SETUP-ERROR  {name}: the neutral change failed the tests")
    total = sum(1 for m in MUTATIONS if m[4])
    print(f"caught {caught}/{total}, survived {survived}, setup errors {setup_errors}")
    return 0 if survived == 0 and setup_errors == 0 else 1


if __name__ == "__main__":
    if not run_tests(HERE):
        print("SETUP-ERROR  the unmodified tests do not pass"); sys.exit(2)
    sys.exit(main())

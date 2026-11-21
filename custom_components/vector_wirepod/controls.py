"""Manual controls for one Vector, sent only when a person asks for them.

No Home Assistant imports, so the policy below is testable on its own.
Added 2026-11-20 at the owner's request ("maybe even manual controls"). wirepod.py stays
read-only apart from its named exceptions; every call that moves the robot, makes it speak
or changes its eyes is in this file, and only these:

1. Only CONTROL_PATHS are requested, each with its own fixed, range-checked arguments
   (PARAM_SPECS). wire-pod pastes some of them into JSON it sends the robot, so nothing
   unchecked is passed.
2. Behavior control is asked for at DEFAULT priority, never OVERRIDE_BEHAVIORS, so Vector
   keeps reacting to cliffs, falls and being picked up while he is driven by hand.
   wire-pod's motion endpoints add their own checks (cliff lockout, time-of-flight stand-off,
   no wheels while on the charger) and those refusals are reported as they are.
3. Control is held only while it is being used: ManualHold lets it go IDLE_RELEASE_S after
   the last command, because holding it keeps the robot awake.
4. Nothing here runs during Quiet mode or Cryosleep (refusal()); both hold the robot still.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

from .wirepod import WirePodError

PATH_ASSUME = "/api-sdk/assume_behavior_control"
PATH_RELEASE = "/api-sdk/release_behavior_control"
PATH_DRIVE = "/api-sdk/drive_straight"
PATH_TURN = "/api-sdk/turn_in_place"
PATH_HEAD = "/api-sdk/set_head_angle"
PATH_LIFT = "/api-sdk/set_lift_height"
PATH_STOP = "/api-sdk/stop_motors"
PATH_SAY = "/api-sdk/say_text"
PATH_EYE = "/api-sdk/eye_color"
PATH_EYE_CUSTOM = "/api-sdk/custom_eye_color"
CONTROL_PATHS = (PATH_ASSUME, PATH_RELEASE, PATH_DRIVE, PATH_TURN, PATH_HEAD, PATH_LIFT,
                 PATH_STOP, PATH_SAY, PATH_EYE, PATH_EYE_CUSTOM)
PRIORITY = "default"

HEAD_MIN_DEG, HEAD_MAX_DEG = -22.0, 45.0      # wire-pod's own clamp (sdkHeadMinDeg/MaxDeg)
LIFT_MIN_PCT, LIFT_MAX_PCT = 0.0, 100.0
SAY_MAX_CHARS = 255                           # wire-pod refuses 600; HA text entities stop at 255
EYE_PRESET_MAX = 6
# (name, minimum, maximum) per path. Numbers outside are refused here, not clamped.
PARAM_SPECS: dict[str, tuple[tuple[str, float, float], ...]] = {
    PATH_DRIVE: (("dist_mm", -150.0, 600.0), ("speed_mmps", 20.0, 160.0)),
    PATH_TURN: (("angle_deg", -360.0, 360.0), ("speed_dps", 15.0, 180.0)),
    PATH_HEAD: (("angle_deg", HEAD_MIN_DEG, HEAD_MAX_DEG),),
    PATH_LIFT: (("height_pct", LIFT_MIN_PCT, LIFT_MAX_PCT),),
    PATH_EYE: (("color", 0.0, float(EYE_PRESET_MAX)),),
    PATH_EYE_CUSTOM: (("hue", 0.0, 1.0), ("sat", 0.0, 1.0)),
}
# One press of a drive button: (path, distance or angle, speed). Positive turns are to the left.
STEPS = {
    "forward": (PATH_DRIVE, 100.0, 75.0),
    "backward": (PATH_DRIVE, -50.0, 50.0),
    "turn_left": (PATH_TURN, 45.0, 90.0),
    "turn_right": (PATH_TURN, -45.0, 90.0),
}
IDLE_RELEASE_S = 60.0
GRANT_WAIT_S = 1.5        # a new hold is answered "success" before the robot has granted it

REASONS = {
    "on_charger": "Vector is on his charger; wire-pod does not drive him there (Explore takes him off it)",
    "picked_up": "Vector is being held",
    "falling": "Vector is falling",
    "cliff_detected": "Vector sees an edge",
    "prox_blocked": "something is right in front of Vector",
    "prox_standoff": "something is in front of Vector",
    "prox_unavailable": "Vector's distance sensor gave no reading",
    "prox_occluded_lift_in_fov": "Vector's lift is in front of his distance sensor; lower it first",
}


class ControlRefused(WirePodError):
    """wire-pod or the robot declined a manual control. The reason is in plain words."""


def _format_number(name: str, value: object, low: float, high: float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number: {value!r}")
    if not low <= float(value) <= high:          # NaN and infinity fail this too
        raise ValueError(f"{name} must be {low} to {high}: {value!r}")
    if name == "color":
        if float(value) != int(value):
            raise ValueError(f"color must be a whole number: {value!r}")
        return str(int(value))
    return repr(float(value))


def check_say_text(text: object) -> str:
    """The text to say, or ValueError. No control characters; 1 to SAY_MAX_CHARS characters."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    clean = text.strip()
    if not 0 < len(clean) <= SAY_MAX_CHARS:
        raise ValueError(f"text must be 1 to {SAY_MAX_CHARS} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean):
        raise ValueError("text must not contain control characters")
    return clean


def control_url(base_url: str, path: str, serial: str, params: dict | None = None) -> str:
    """URL of one manual control. Refuses any path or argument that is not exactly allowed."""
    if path not in CONTROL_PATHS:
        raise ValueError(f"path not allowed: {path!r}")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be http(s)")
    if not isinstance(serial, str) or not serial.isalnum():
        raise ValueError(f"invalid serial: {serial!r}")
    given = dict(params or {})
    url = base_url.rstrip("/") + path + "?serial=" + quote(serial)
    if path == PATH_ASSUME:
        if given:
            raise ValueError("assume takes no arguments; its priority is fixed")
        return url + "&priority=" + PRIORITY
    if path == PATH_SAY:
        if set(given) != {"text"}:
            raise ValueError("say_text takes exactly text")
        return url + "&text=" + quote(check_say_text(given["text"]), safe="")
    specs = PARAM_SPECS.get(path, ())
    if set(given) != {name for name, _, _ in specs}:
        raise ValueError(f"{path} takes exactly {[name for name, _, _ in specs]}")
    for name, low, high in specs:
        url += f"&{name}=" + _format_number(name, given[name], low, high)
    return url


def reason_text(raw: str) -> str:
    """wire-pod's refusal in plain words. Unknown ones are passed on, shortened."""
    reason = raw.strip()
    if reason.lower().startswith("error:"):
        reason = reason[6:].strip()
    if reason.startswith("cliff_lockout"):
        return "Vector stopped at an edge; turn him or back up before driving forward"
    return REASONS.get(reason, reason[:160] or "no reason given")


def parse_answer(status: int, text: str, label: str) -> dict:
    """wire-pod answers 200 even for refusals: 'error: ...' text, JSON with ok, or a plain word."""
    if status != 200:
        raise WirePodError(f"{label}: http {status}: {text.strip()[:120]}")
    body = text.strip()
    if body.lower().startswith("error"):
        raise ControlRefused(reason_text(body))
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise WirePodError(f"{label}: not JSON: {body[:80]!r}") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ControlRefused(reason_text(str(data.get("error", "")) if isinstance(data, dict) else ""))
        return data
    return {"ok": True, "answer": body[:40]}


async def send(session: aiohttp.ClientSession, base_url: str, serial: str, path: str,
               params: dict | None = None, timeout_s: float = 15.0) -> dict:
    """Send one manual control and return wire-pod's answer, or raise."""
    assert timeout_s > 0
    url = control_url(base_url, path, serial, params)
    tmo = aiohttp.ClientTimeout(total=timeout_s)
    async with session.post(url, timeout=tmo) as resp:
        text = await resp.text()
        return parse_answer(resp.status, text, path.rsplit("/", 1)[-1])


def refusal(quiet_on: bool | None, cryosleep_on: bool, reachable: bool) -> str | None:
    """Why manual controls cannot run now, or None when they can."""
    if cryosleep_on:
        return "Cryosleep is on; turn it off first"
    if quiet_on:
        return "Quiet mode is on; turn it off to use manual controls"
    if not reachable:
        return "Vector is not reachable through wire-pod"
    return None


@dataclass
class ManualHold:
    """Whether Home Assistant holds behavior control of one robot, and until when."""

    idle_s: float = IDLE_RELEASE_S
    held: bool = False
    since: float = 0.0
    last_command: float = 0.0

    def __post_init__(self) -> None:
        assert self.idle_s > 0

    def begin(self, now: float) -> bool:
        """A command is about to run. True when control has to be asked for first."""
        assert now >= 0
        fresh = not self.held
        if fresh:
            self.held, self.since = True, now
        self.last_command = now
        return fresh

    def release_at(self) -> float | None:
        return self.last_command + self.idle_s if self.held else None

    def release_due(self, now: float) -> bool:
        due = self.release_at()
        return due is not None and now >= due

    def end(self) -> bool:
        """Control is let go. True when it was held."""
        was = self.held
        self.held = False
        return was

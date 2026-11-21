"""Read-only robot data passed through to Home Assistant: lifetime stats and settings.

No Home Assistant imports, so it is testable on its own.

Both reads are jdoc pulls through wire-pod. They change nothing on the robot and do not
wake a sleeping one (measured 2026-11-20: a robot answered both while reporting asleep).

Units, from the robot's own source (os-vector/wire-os-victor,
engine/components/robotStatsTracker.cpp):
- "Alive.seconds" adds 60 s per minute the robot is switched on: time on, not age.
- "Pet.ms" is petting time in milliseconds.
- "Odom.Body" is body travel in millimetres times kOdomFixedPoint (100000).
"""

from __future__ import annotations

import json
from urllib.parse import quote

import aiohttp

from .wirepod import WirePodError, build_url

STATS_PATH = "/api-sdk/get_robot_stats"
SETTINGS_PATH = "/api-sdk/get_sdk_settings"
ODOM_FIXED_POINT = 100000.0
MAX_STAT_KEYS = 200
MAX_KEY_CHARS = 80
MAX_TEXT_CHARS = 120

# EyeColor in the SDK's settings.proto, in enum order (0 = TIP_OVER_TEAL ... 6 = CONFUSION_MATRIX_GREEN).
EYE_PRESETS = ("Teal", "Orange", "Yellow", "Lime", "Sapphire", "Purple", "Green")
EYE_CUSTOM = "Custom"

SETTING_BOOLS = ("clock_24_hour", "dist_is_metric", "temp_is_fahrenheit")
SETTING_TEXTS = ("locale", "time_zone", "default_location")


def _number(value: object) -> float | None:
    """A real int or float, never a bool. None otherwise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def stats_url(base_url: str, serial: str) -> str:
    """URL of the lifetime stats read, checked the same way build_url checks its own."""
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url must be http(s)")
    if not isinstance(serial, str) or not serial.isalnum():
        raise ValueError(f"invalid serial: {serial!r}")
    return base_url.rstrip("/") + STATS_PATH + "?serial=" + quote(serial)


def normalize_stats(raw: object) -> dict:
    """Lifetime stats: named values in real units, plus every numeric counter as passed through."""
    if not isinstance(raw, dict):
        raise WirePodError("robot stats: not a JSON object")
    counters: dict[str, float] = {}
    for key in list(raw)[:MAX_STAT_KEYS]:
        value = _number(raw[key])
        if isinstance(key, str) and 0 < len(key) <= MAX_KEY_CHARS and value is not None:
            counters[key] = value
    alive = counters.get("Alive.seconds")
    pet = counters.get("Pet.ms")
    odom = counters.get("Odom.Body")
    reacted = counters.get("BStat.ReactedToTriggerWord")
    return {
        "time_on_s": None if alive is None else int(alive),
        "petted_s": None if pet is None else round(pet / 1000.0, 1),
        "distance_m": None if odom is None else round(odom / ODOM_FIXED_POINT / 1000.0, 2),
        "wake_word_reactions": None if reacted is None else int(reacted),
        "counters": counters,
    }


def _eye(raw: dict) -> dict:
    """Eye colour as a preset name or 'Custom', with the custom hue and saturation (0 to 1)."""
    custom = raw.get("custom_eye_color")
    hue = sat = None
    custom_on = False
    if isinstance(custom, dict):
        hue, sat = _number(custom.get("hue")), _number(custom.get("saturation"))
        custom_on = custom.get("enabled") is True and hue is not None and sat is not None
    preset = raw.get("eye_color")
    preset_ok = isinstance(preset, int) and not isinstance(preset, bool) and 0 <= preset < len(EYE_PRESETS)
    if custom_on:
        name = EYE_CUSTOM
    else:
        name = EYE_PRESETS[preset] if preset_ok else None
    return {"eye_color": name, "eye_preset": preset if preset_ok else None,
            "custom_hue": hue, "custom_saturation": sat}


def normalize_settings(raw: object) -> dict:
    """The robot's settings that are safe and useful to show. Unknown or malformed ones are dropped."""
    if not isinstance(raw, dict):
        raise WirePodError("robot settings: not a JSON object")
    out: dict = _eye(raw)
    assert out["eye_color"] in (None, EYE_CUSTOM, *EYE_PRESETS)
    for key in SETTING_BOOLS:
        out[key] = raw[key] if isinstance(raw.get(key), bool) else None
    for key in SETTING_TEXTS:
        value = raw.get(key)
        out[key] = value[:MAX_TEXT_CHARS] if isinstance(value, str) and value else None
    return out


async def _get_json(session: aiohttp.ClientSession, url: str, label: str, timeout_s: float) -> object:
    """GET a wire-pod jdoc answer. wire-pod reports robot errors as 200 with 'error: ...' text."""
    assert timeout_s > 0 and bool(label)
    tmo = aiohttp.ClientTimeout(total=timeout_s)
    async with session.get(url, timeout=tmo) as resp:
        text = await resp.text()
        if resp.status != 200:
            raise WirePodError(f"{label}: http {resp.status}: {text.strip()[:120]}")
    if text.lstrip().startswith("error"):
        raise WirePodError(f"{label}: {text.strip()[:120]}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise WirePodError(f"{label}: not JSON: {text[:80]!r}") from exc


async def get_stats(session: aiohttp.ClientSession, base_url: str, serial: str,
                    timeout_s: float = 20.0) -> dict:
    """Lifetime stats of one robot (read only)."""
    return normalize_stats(await _get_json(session, stats_url(base_url, serial), "robot stats", timeout_s))


async def get_settings(session: aiohttp.ClientSession, base_url: str, serial: str,
                       timeout_s: float = 20.0) -> dict:
    """Settings of one robot (read only)."""
    url = build_url(base_url, SETTINGS_PATH, serial)
    return normalize_settings(await _get_json(session, url, "robot settings", timeout_s))

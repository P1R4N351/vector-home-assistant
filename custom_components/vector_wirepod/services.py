"""Home Assistant service registrations for Vector (domain vector_wirepod)."""

from __future__ import annotations

from typing import TYPE_CHECKING

try:
    import voluptuous as vol
except ModuleNotFoundError:  # unit tests run without voluptuous when HA is stubbed
    vol = None  # type: ignore[assignment]

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import controls
from .const import CONF_SERIAL, DOMAIN
from .manual import ensure_control

if TYPE_CHECKING:
    from homeassistant.core import ServiceCall

SERVICE_SAY = "say"
SERVICE_DRIVE = "drive"
SERVICE_TURN = "turn"
SERVICE_SET_HEAD = "set_head"
SERVICE_SET_LIFT = "set_lift"
SERVICE_STOP = "stop"
ALL_SERVICES = (
    SERVICE_SAY,
    SERVICE_DRIVE,
    SERVICE_TURN,
    SERVICE_SET_HEAD,
    SERVICE_SET_LIFT,
    SERVICE_STOP,
)
MAX_ENTRIES_SCAN = 64


def _resolve_entry(hass: HomeAssistant, serial: str | None):
    """Return the loaded ConfigEntry matching serial, or the sole robot when serial is omitted."""
    assert hass is not None
    entries = [
        entry
        for entry in list(hass.config_entries.async_entries(DOMAIN))[:MAX_ENTRIES_SCAN]
        if getattr(entry, "runtime_data", None) is not None
    ]
    if not entries:
        raise HomeAssistantError("No Vector robot is currently loaded")
    want = serial.strip().lower() if isinstance(serial, str) and serial.strip() else None
    if want is not None:
        for entry in entries:
            if str(entry.data.get(CONF_SERIAL, "")).lower() == want:
                return entry
        raise HomeAssistantError(f"Unknown Vector serial: {serial}")
    if len(entries) == 1:
        return entries[0]
    serials = ", ".join(str(e.data.get(CONF_SERIAL, "")) for e in entries)
    raise HomeAssistantError(f"Multiple Vector robots configured; pass serial ({serials})")


def _build_schemas() -> dict[str, object]:
    """Return voluptuous schemas keyed by service name, or None when voluptuous is absent."""
    if vol is None:
        return {name: None for name in ALL_SERVICES}
    opt_serial = {vol.Optional(CONF_SERIAL): str}
    return {
        SERVICE_SAY: vol.Schema({vol.Required("text"): str, **opt_serial}),
        SERVICE_DRIVE: vol.Schema({
            vol.Required("distance_mm"): vol.Coerce(float),
            vol.Optional("speed_mmps", default=75.0): vol.Coerce(float),
            **opt_serial,
        }),
        SERVICE_TURN: vol.Schema({
            vol.Required("angle_deg"): vol.Coerce(float),
            vol.Optional("speed_dps", default=90.0): vol.Coerce(float),
            **opt_serial,
        }),
        SERVICE_SET_HEAD: vol.Schema({vol.Required("angle_deg"): vol.Coerce(float), **opt_serial}),
        SERVICE_SET_LIFT: vol.Schema({vol.Required("height_pct"): vol.Coerce(float), **opt_serial}),
        SERVICE_STOP: vol.Schema(opt_serial),
    }


def _control_for_call(hass: HomeAssistant, data: dict):
    entry = _resolve_entry(hass, data.get(CONF_SERIAL))
    return ensure_control(hass, entry)


def _check_float(name: str, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise HomeAssistantError(f"{name} must be a number")
    return float(raw)


async def _svc_say(hass: HomeAssistant, call: ServiceCall) -> None:
    control = _control_for_call(hass, call.data)
    try:
        text = controls.check_say_text(call.data.get("text"))
    except ValueError as exc:
        raise HomeAssistantError(str(exc)) from exc
    await control.say(text)
    control.last_said = text
    control.notify()


async def _svc_drive(hass: HomeAssistant, call: ServiceCall) -> None:
    control = _control_for_call(hass, call.data)
    dist = _check_float("distance_mm", call.data.get("distance_mm"))
    speed = _check_float("speed_mmps", call.data.get("speed_mmps", 75.0))
    await control.command(controls.PATH_DRIVE, {"dist_mm": dist, "speed_mmps": speed})


async def _svc_turn(hass: HomeAssistant, call: ServiceCall) -> None:
    control = _control_for_call(hass, call.data)
    angle = _check_float("angle_deg", call.data.get("angle_deg"))
    speed = _check_float("speed_dps", call.data.get("speed_dps", 90.0))
    await control.command(controls.PATH_TURN, {"angle_deg": angle, "speed_dps": speed})


async def _svc_head(hass: HomeAssistant, call: ServiceCall) -> None:
    control = _control_for_call(hass, call.data)
    angle = _check_float("angle_deg", call.data.get("angle_deg"))
    answer = await control.command(controls.PATH_HEAD, {"angle_deg": angle})
    got = answer.get("angle_deg")
    control.head_deg = float(got) if isinstance(got, (int, float)) else angle
    control.notify()


async def _svc_lift(hass: HomeAssistant, call: ServiceCall) -> None:
    control = _control_for_call(hass, call.data)
    height = _check_float("height_pct", call.data.get("height_pct"))
    answer = await control.command(controls.PATH_LIFT, {"height_pct": height})
    got = answer.get("height_pct")
    control.lift_pct = float(got) if isinstance(got, (int, float)) else height
    control.notify()


async def _svc_stop(hass: HomeAssistant, call: ServiceCall) -> None:
    control = _control_for_call(hass, call.data)
    await control.stop()


def _bind_handler(hass: HomeAssistant, fn):
    async def _wrapped(call: ServiceCall) -> None:
        await fn(hass, call)

    return _wrapped


def async_register_services(hass: HomeAssistant) -> None:
    """Register vector_wirepod services once per Home Assistant instance."""
    services = getattr(hass, "services", None)
    if services is None or services.has_service(DOMAIN, SERVICE_SAY):
        return
    schemas = _build_schemas()
    impls = {
        SERVICE_SAY: _svc_say,
        SERVICE_DRIVE: _svc_drive,
        SERVICE_TURN: _svc_turn,
        SERVICE_SET_HEAD: _svc_head,
        SERVICE_SET_LIFT: _svc_lift,
        SERVICE_STOP: _svc_stop,
    }
    for name in ALL_SERVICES:
        services.async_register(DOMAIN, name, _bind_handler(hass, impls[name]), schema=schemas[name])


def async_unregister_services(hass: HomeAssistant, unloading_entry_id: str) -> None:
    """Remove vector_wirepod services once the last loaded config entry unloads."""
    services = getattr(hass, "services", None)
    if services is None:
        return
    entries_fn = getattr(getattr(hass, "config_entries", None), "async_entries", None)
    if callable(entries_fn):
        remaining = [
            e for e in list(entries_fn(DOMAIN))[:MAX_ENTRIES_SCAN]
            if getattr(e, "entry_id", None) != unloading_entry_id
            and getattr(e, "runtime_data", None) is not None
        ]
        if remaining:
            return
    for name in ALL_SERVICES:
        if services.has_service(DOMAIN, name):
            services.async_remove(DOMAIN, name)

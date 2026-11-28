"""Vector: one Vector robot in Home Assistant, through wire-pod.

The robot-facing policy lives in wirepod.py (camera, status and the named
exceptions) and controls.py (manual controls, sent only when a person asks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (BATTERY_POLL_S, BATTERY_TIMEOUT_S, CONF_BATTERY_POLL_S,
                    CONF_FALLBACK_URL, CONF_SERIAL, CONF_WIREPOD_URL, CYCLE_POLL_S,
                    DOMAIN, FAILOVER_CONNECT_TIMEOUT_S, MIN_BATTERY_POLL_S,
                    PLATFORMS, PRIMARY_RETRY_S, SIGNAL_RECOVERED)
from . import hardware
from .failover import FailoverSession
from .manager import VectorManager
from .services import async_register_services, async_unregister_services
from .sleepcycle import GOING, IDLE
from .wirepod import (CameraHub, HubConfig, WirePodError, get_battery,
                      get_quiet_mode, get_robot_state)

_LOGGER = logging.getLogger(__name__)


@dataclass
class VectorData:
    hub: CameraHub
    coordinator: DataUpdateCoordinator
    session: aiohttp.ClientSession
    base_url: str
    serial: str
    manager: VectorManager
    manual: object = None        # manual.ManualControl, made by the first platform that needs it
    extras: object = None        # the slow data coordinator, made with it


def _sync_device_model(hass: HomeAssistant, entry_id: str, serial: str, hub: CameraHub) -> None:
    """Show the robot as what its camera says it is (Anki Vector, or Vector 2.0)."""
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, serial), entry_id)
    if device is None:
        return
    maker, model = hardware.maker_and_model(serial, hub.frame_size())
    if device.manufacturer != maker or device.model != model:
        registry.async_update_device(device.id, manufacturer=maker, model=model)


async def _read_robot_state(session: aiohttp.ClientSession, base: str, serial: str) -> dict | None:
    """The robot's own flags, or None when they could not be read this time."""
    try:
        return await get_robot_state(session, base, serial)
    except (WirePodError, aiohttp.ClientError, TimeoutError) as exc:
        _LOGGER.debug("robot_state poll error for %s: %s", serial, exc)
        return None


def _handle_pod_unreachable(coordinator: DataUpdateCoordinator, hub: CameraHub,
                            manager: VectorManager, poll_s: int, exc: Exception) -> None:
    """Record pod outage, keep Cryosleep state machine synced, and keep fast recovery polling."""
    hub.set_robot_reachable(False, "wire-pod unreachable")
    manager.observe(None, robot_reachable=False)
    busy = manager.cycle.phase != IDLE or manager.cryosleep.enabled
    coordinator.update_interval = timedelta(seconds=min(poll_s, CYCLE_POLL_S) if busy else poll_s)
    raise UpdateFailed(f"wire-pod unreachable: {exc}") from exc


def _make_coordinator(hass: HomeAssistant, entry: ConfigEntry, session: object,
                      hub: CameraHub, manager: VectorManager) -> DataUpdateCoordinator:
    assert hass is not None and entry is not None
    assert session is not None and hub is not None and manager is not None
    base = entry.data[CONF_WIREPOD_URL]
    serial = entry.data[CONF_SERIAL]
    poll_s = max(MIN_BATTERY_POLL_S, int(entry.options.get(CONF_BATTERY_POLL_S, BATTERY_POLL_S)))

    async def _update() -> dict:
        was_reachable = bool(isinstance(coordinator.data, dict) and coordinator.data.get("robot_reachable"))
        quiet: dict | None = None
        try:
            quiet = await get_quiet_mode(session, base, serial)
        except WirePodError as exc:
            _LOGGER.debug("quiet_mode poll error for %s: %s", serial, exc)
        except (aiohttp.ClientError, TimeoutError) as exc:
            _handle_pod_unreachable(coordinator, hub, manager, poll_s, exc)
        try:
            data = await get_battery(session, base, serial, BATTERY_TIMEOUT_S)
        except WirePodError as exc:
            hub.set_robot_reachable(False, f"robot not answering wire-pod: {exc}"[:200])
            manager.observe(None, robot_reachable=False)
            return {"robot_reachable": False, "error": str(exc)[:200], "quiet_mode": quiet}
        except (aiohttp.ClientError, TimeoutError) as exc:
            _handle_pod_unreachable(coordinator, hub, manager, poll_s, exc)
        return await _finish_reachable_poll(
            hass, coordinator, hub, manager, session, entry.entry_id, base, serial,
            data, quiet, poll_s, was_reachable)
    coordinator = DataUpdateCoordinator(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {serial}",
                                        update_interval=timedelta(seconds=poll_s), update_method=_update)
    manager.coordinator = coordinator
    return coordinator


async def _finish_reachable_poll(hass: HomeAssistant, coordinator: DataUpdateCoordinator,
                                 hub: CameraHub, manager: VectorManager, session: object,
                                 entry_id: str, base: str, serial: str, data: dict,
                                 quiet: dict | None, poll_s: int, was_reachable: bool) -> dict:
    """Update hub, Cryosleep/cycle state, and notify polling entities on outage recovery."""
    hub.set_robot_reachable(True)
    _sync_device_model(hass, entry_id, serial, hub)
    data["robot_reachable"] = True
    data["quiet_mode"] = quiet
    data["robot_state"] = await _read_robot_state(session, base, serial)
    coordinator.data = data            # the cycle's actions read quiet mode from here
    manager.observe(data["robot_state"], robot_reachable=True)
    state = data["robot_state"]
    hub.set_robot_asleep(bool(state and state.get("asleep")) or manager.cycle.phase == GOING or manager.cryosleep.enabled)
    busy = manager.cycle.phase != IDLE or manager.cryosleep.enabled
    coordinator.update_interval = timedelta(seconds=min(poll_s, CYCLE_POLL_S) if busy else poll_s)
    if not was_reachable:
        async_dispatcher_send(hass, SIGNAL_RECOVERED.format(serial))
    return data


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    assert hass is not None and entry is not None
    raw_session = async_get_clientsession(hass)
    fallback_url = (entry.options.get(CONF_FALLBACK_URL) or entry.data.get(CONF_FALLBACK_URL) or "").strip()
    session = FailoverSession(
        raw_session,
        entry.data[CONF_WIREPOD_URL],
        fallback_url,
        primary_retry_s=PRIMARY_RETRY_S,
        connect_timeout_s=FAILOVER_CONNECT_TIMEOUT_S,
    )
    hub = CameraHub(session, HubConfig(base_url=entry.data[CONF_WIREPOD_URL],
                                       serial=entry.data[CONF_SERIAL]))
    manager = VectorManager(hass, entry, session, hub)
    coordinator = _make_coordinator(hass, entry, session, hub, manager)
    await coordinator.async_refresh()
    entry.runtime_data = VectorData(
        hub=hub,
        coordinator=coordinator,
        session=session,
        base_url=entry.data[CONF_WIREPOD_URL],
        serial=entry.data[CONF_SERIAL],
        manager=manager,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    assert hass is not None and entry is not None
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        manual = getattr(entry.runtime_data, "manual", None)
        if manual is not None:
            await manual.shutdown()        # lets go of behavior control if it is held
        await entry.runtime_data.hub.close()
        async_unregister_services(hass, entry.entry_id)
    return ok

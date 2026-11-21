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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (BATTERY_POLL_S, BATTERY_TIMEOUT_S, CONF_BATTERY_POLL_S,
                    CONF_SERIAL, CONF_WIREPOD_URL, CYCLE_POLL_S, DOMAIN,
                    MIN_BATTERY_POLL_S, PLATFORMS)
from . import hardware
from .manager import VectorManager
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


def _make_coordinator(hass: HomeAssistant, entry: ConfigEntry, session: aiohttp.ClientSession,
                      hub: CameraHub, manager: VectorManager) -> DataUpdateCoordinator:
    assert hass is not None and entry is not None
    assert session is not None and hub is not None and manager is not None
    base = entry.data[CONF_WIREPOD_URL]
    serial = entry.data[CONF_SERIAL]
    poll_s = max(MIN_BATTERY_POLL_S, int(entry.options.get(CONF_BATTERY_POLL_S, BATTERY_POLL_S)))

    async def _update() -> dict:
        quiet: dict | None = None
        try:
            quiet = await get_quiet_mode(session, base, serial)
        except WirePodError as exc:
            _LOGGER.debug("quiet_mode poll error for %s: %s", serial, exc)
        except (aiohttp.ClientError, TimeoutError) as exc:
            hub.set_robot_reachable(False, "wire-pod unreachable")
            raise UpdateFailed(f"wire-pod unreachable: {exc}") from exc
        try:
            data = await get_battery(session, base, serial, BATTERY_TIMEOUT_S)
        except WirePodError as exc:
            hub.set_robot_reachable(False, f"robot not answering wire-pod: {exc}"[:200])
            manager.observe(None, robot_reachable=False)
            return {"robot_reachable": False, "error": str(exc)[:200], "quiet_mode": quiet}
        except (aiohttp.ClientError, TimeoutError) as exc:
            hub.set_robot_reachable(False, "wire-pod unreachable")
            raise UpdateFailed(f"wire-pod unreachable: {exc}") from exc
        hub.set_robot_reachable(True)
        _sync_device_model(hass, entry.entry_id, serial, hub)
        data["robot_reachable"] = True
        data["quiet_mode"] = quiet
        data["robot_state"] = await _read_robot_state(session, base, serial)
        coordinator.data = data            # the cycle's actions read quiet mode from here
        manager.observe(data["robot_state"], robot_reachable=True)
        state = data["robot_state"]
        hub.set_robot_asleep(bool(state and state.get("asleep")) or manager.cycle.phase == GOING or manager.cryosleep.enabled)
        busy = manager.cycle.phase != IDLE or manager.cryosleep.enabled
        coordinator.update_interval = timedelta(seconds=min(poll_s, CYCLE_POLL_S) if busy else poll_s)
        return data
    coordinator = DataUpdateCoordinator(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {serial}",
                                        update_interval=timedelta(seconds=poll_s), update_method=_update)
    manager.coordinator = coordinator
    return coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    assert hass is not None and entry is not None
    session = async_get_clientsession(hass)
    hub = CameraHub(session, HubConfig(base_url=entry.data[CONF_WIREPOD_URL],
                                       serial=entry.data[CONF_SERIAL]))
    manager = VectorManager(hass, entry, session, hub)
    coordinator = _make_coordinator(hass, entry, session, hub, manager)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = VectorData(
        hub=hub,
        coordinator=coordinator,
        session=session,
        base_url=entry.data[CONF_WIREPOD_URL],
        serial=entry.data[CONF_SERIAL],
        manager=manager,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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
    return ok

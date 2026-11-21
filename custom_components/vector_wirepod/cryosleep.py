"""Cryosleep orchestration for one Vector.

Pure state machine and async pod-call orchestrator with no Home Assistant imports,
so entry, poll enforcement, fallback movement lock, and morning restore are testable alone.

Hardware order guarantees (verified against wire-os-victor and a wire-pod server):
- Displace Quiet mode before taking the state snapshot or sending INTENT_SLEEP, so
  RESERVE_CONTROL is released, wire-pod restores any saved volume to ROBOT_SETTINGS,
  and Vector's sleep behavior is allowed to activate (and dock if off-charger).
- Apply runtime cryo_mute BEFORE sleep or backlight: /api-sdk/cryo_mute gates voice
  streams in preqs and toggles vic-anim/vic-engine consolevars + Wwise master gain
  in <20 ms with no anki-robot.target restart, no wake, and no backpack privacy LEDs,
  while leaving ROBOT_SETTINGS master_volume and persistent micMuted untouched.
- Re-zero face backlight when the robot settles asleep (vic-anim uses 5 on sleep entry).
- If sleep fails repeatedly overnight, engage RESERVE_CONTROL (quiet mode) to lock tracks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .const import (
    CRYOSLEEP_GUARD_S,
    CRYOSLEEP_MAX_SLEEP_RETRIES,
    CRYOSLEEP_SETTLE_S,
    DEFAULT_AWAKE_BACKLIGHT,
    QUIET_SETTLE_S,
)
from .wirepod import BACKLIGHT_MAX

PHASE_OFF = "off"
PHASE_ENTERING = "entering"
PHASE_WAITING_ROBOT = "waiting_for_robot"
PHASE_ACTIVE = "active"
PHASE_HOLDING_STILL = "holding_still"
PHASE_EXITING = "exiting"
ALL_CRYO_PHASES = (
    PHASE_OFF,
    PHASE_ENTERING,
    PHASE_WAITING_ROBOT,
    PHASE_ACTIVE,
    PHASE_HOLDING_STILL,
    PHASE_EXITING,
)

POLL_NOOP = "noop"
POLL_APPLY = "apply"
POLL_MARK_OFFLINE = "mark_offline"
POLL_SETTLED_ASLEEP = "settled_asleep"
POLL_REENFORCE_SLEEP = "reenforce_sleep"
POLL_LOCK_STILL = "lock_still"
POLL_DRIFT_GUARD = "drift_guard"

_OPT_INT = lambda v, lo, hi: v if (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi) else None


def cryosleep_displaced(manager: object) -> bool:
    """True while Cryosleep is armed and displaces sibling controls."""
    cryo = getattr(manager, "cryosleep", None)
    return bool(cryo is not None and cryo.enabled)


def _cryo_busy(manager: object) -> bool:
    """True while Cryosleep is active or still running exit hardware restore."""
    cryo = getattr(manager, "cryosleep", None)
    return bool(cryo is not None and (cryo.enabled or cryo.phase == PHASE_EXITING))


def guard_not_cryosleep(manager: object, exc_cls: type[Exception]) -> None:
    """Raise exc_cls when Cryosleep displaces manual robot controls."""
    assert exc_cls is not None and issubclass(exc_cls, Exception)
    if cryosleep_displaced(manager):
        raise exc_cls("Cryosleep is active; turn off Cryosleep first")


def bind_cryosleep_entity(entity: object, manager: object, *, refresh_on_exit: bool = False) -> None:
    """Subscribe an entity to Cryosleep transitions so availability and reads stay current."""
    assert entity is not None and isinstance(refresh_on_exit, bool)
    add_fn = getattr(manager, "add_cryosleep_listener", None)
    if not callable(add_fn):
        return
    was_displaced = cryosleep_displaced(manager)
    was_busy = _cryo_busy(manager)

    def _on_change() -> None:
        nonlocal was_displaced, was_busy
        now_displaced = cryosleep_displaced(manager)
        now_busy = _cryo_busy(manager)
        entity.async_write_ha_state()  # type: ignore[attr-defined]
        phase = getattr(getattr(manager, "cryosleep", None), "phase", PHASE_OFF)
        exit_done = (was_busy and not now_busy) or (
            was_displaced and not now_displaced and phase == PHASE_OFF
        )
        if refresh_on_exit and exit_done:
            entity.async_schedule_update_ha_state(force_refresh=True)  # type: ignore[attr-defined]
        was_displaced = now_displaced
        was_busy = now_busy

    entity.async_on_remove(add_fn(_on_change))  # type: ignore[attr-defined]


@dataclass
class CryosleepState:
    enabled: bool = False
    phase: str = PHASE_OFF
    applied: bool = False
    cryo_muted: bool = False
    was_asleep: bool = False
    awake_polls: int = 0
    holding_reserve: bool = False
    sleep_retries: int = 0
    last_sleep_at: float = 0.0
    last_guard_at: float = 0.0
    enforcements: int = 0
    last_error: str = ""
    snapshot_taken: bool = False
    saved_backlight: int | None = None
    saved_camera_allowed: bool | None = None

    @property
    def displaces_controls(self) -> bool:
        return self.enabled

    def restore_camera_target(self) -> bool:
        """Return saved camera_allowed boolean without clearing snapshot state."""
        return True if self.saved_camera_allowed is None else bool(self.saved_camera_allowed)

    def arm(self, camera_allowed: bool) -> None:
        """Arm Cryosleep from enabled state, capturing initial camera_allowed if not yet snapshotted."""
        assert isinstance(camera_allowed, bool)
        assert self.phase in ALL_CRYO_PHASES
        self.enabled, self.phase, self.applied, self.cryo_muted = True, PHASE_ENTERING, False, False
        self.was_asleep, self.awake_polls, self.sleep_retries, self.last_error = False, 0, 0, ""
        if not self.snapshot_taken:
            self.saved_camera_allowed = camera_allowed

    def begin_exit(self) -> None:
        """Mark Cryosleep turning off while exit cleanup runs."""
        assert self.phase in ALL_CRYO_PHASES
        self.enabled = False
        self.phase = PHASE_EXITING

    def finish_exit(self) -> bool:
        """Reset Cryosleep state and return saved camera_allowed boolean to restore."""
        cam = self.restore_camera_target()
        self.enabled, self.phase, self.applied, self.cryo_muted = False, PHASE_OFF, False, False
        self.was_asleep, self.awake_polls, self.holding_reserve, self.sleep_retries = False, 0, False, 0
        self.last_sleep_at, self.last_guard_at, self.last_error = 0.0, 0.0, ""
        self.snapshot_taken = False
        self.saved_backlight = self.saved_camera_allowed = None
        assert self.phase == PHASE_OFF
        return cam

    def record_snapshot(self, backlight: int | None) -> None:
        """Store pre-cryosleep screen level once per sleep session without overwriting later."""
        assert self.phase in ALL_CRYO_PHASES
        if self.snapshot_taken:
            return
        self.saved_backlight = _OPT_INT(backlight, 0, BACKLIGHT_MAX)
        self.snapshot_taken = True
        assert self.snapshot_taken

    def restore_from_attrs(self, attrs: dict | None, fallback_camera_allowed: bool) -> None:
        """Restore Cryosleep and saved pre-sleep state from Home Assistant RestoreEntity attrs."""
        assert isinstance(fallback_camera_allowed, bool)
        src = attrs if isinstance(attrs, dict) else {}
        self.enabled, self.phase, self.applied = True, PHASE_ENTERING, False
        self.cryo_muted = bool(src.get("cryo_muted", False))
        self.was_asleep, self.awake_polls, self.sleep_retries, self.last_error = False, 0, 0, ""
        self.holding_reserve = bool(src.get("holding_reserve", False))
        enf = src.get("enforcements", 0)
        self.enforcements = enf if isinstance(enf, int) and not isinstance(enf, bool) and enf >= 0 else 0
        self.saved_backlight = _OPT_INT(src.get("saved_backlight"), 0, BACKLIGHT_MAX)
        cam = src.get("saved_camera_allowed")
        self.saved_camera_allowed = cam if isinstance(cam, bool) else fallback_camera_allowed
        self.snapshot_taken = bool(src.get("snapshot_taken", False))
        assert self.enabled is True

    def target_restore_backlight(self, robot_asleep: bool | None) -> int:
        assert robot_asleep is None or isinstance(robot_asleep, bool)
        if robot_asleep is True:
            return 0
        if self.saved_backlight is not None and self.saved_backlight > 0:
            return self.saved_backlight
        return DEFAULT_AWAKE_BACKLIGHT

    def as_attributes(self) -> dict[str, object]:
        return {
            "phase": self.phase, "applied": self.applied, "cryo_muted": self.cryo_muted,
            "holding_reserve": self.holding_reserve, "enforcements": self.enforcements,
            "snapshot_taken": self.snapshot_taken, "saved_backlight": self.saved_backlight,
            "saved_camera_allowed": self.saved_camera_allowed, "last_error": self.last_error,
        }

    def decide_poll(self, reachable: bool, asleep: bool | None, now: float, *,
                    settle_s: float = CRYOSLEEP_SETTLE_S, guard_s: float = CRYOSLEEP_GUARD_S,
                    max_retries: int = CRYOSLEEP_MAX_SLEEP_RETRIES) -> str:
        """Pure poll transition deciding whether Cryosleep needs pod actuation this tick."""
        assert settle_s > 0 and guard_s > 0 and max_retries >= 1
        if not self.enabled:
            return POLL_NOOP
        if not reachable:
            if self.phase != PHASE_WAITING_ROBOT or self.applied:
                self.phase, self.applied = PHASE_WAITING_ROBOT, False
                self.last_error = "robot unreachable; cryosleep armed"
                return POLL_MARK_OFFLINE
            return POLL_NOOP
        if not self.applied:
            return POLL_APPLY
        if asleep is True:
            self.awake_polls = 0
            if not self.was_asleep or self.holding_reserve:
                self.was_asleep, self.sleep_retries, self.phase = True, 0, PHASE_ACTIVE
                return POLL_SETTLED_ASLEEP
        elif asleep is False:
            return self._decide_awake_poll(now, settle_s, max_retries)
        else:
            self.awake_polls = 0
        if now - self.last_guard_at >= guard_s:
            self.last_guard_at = now
            return POLL_DRIFT_GUARD
        return POLL_NOOP

    def _decide_awake_poll(self, now: float, settle_s: float, max_retries: int) -> str:
        assert now >= 0 and max_retries >= 1
        if self.was_asleep:
            self.awake_polls += 1
            if self.awake_polls < 2:
                return POLL_NOOP
            self.was_asleep, self.awake_polls, self.sleep_retries = False, 0, 1
            self.last_sleep_at, self.phase = now, PHASE_ACTIVE
            self.enforcements += 1
            return POLL_REENFORCE_SLEEP
        if now - self.last_sleep_at >= settle_s:
            if self.sleep_retries < max_retries:
                self.sleep_retries += 1
                self.last_sleep_at = now
                self.enforcements += 1
                return POLL_REENFORCE_SLEEP
            if not self.holding_reserve:
                self.holding_reserve, self.phase = True, PHASE_HOLDING_STILL
                self.enforcements += 1
                return POLL_LOCK_STILL
        return POLL_NOOP


@dataclass(frozen=True)
class CryosleepIO:
    get_quiet: Callable[[], Awaitable[dict | None]]
    set_quiet: Callable[[bool], Awaitable[None]]
    get_backlight: Callable[[], Awaitable[int | None]]
    set_backlight: Callable[[int | None], Awaitable[None]] | Callable[[int], Awaitable[int | None]]
    get_cryo_mute: Callable[[], Awaitable[dict | None]]
    set_cryo_mute: Callable[[bool], Awaitable[dict | None]]
    get_robot_state: Callable[[], Awaitable[dict | None]]
    send_sleep: Callable[[], Awaitable[None]]
    sleep: Callable[[float], Awaitable[None]]
    now: Callable[[], float]
    failures: tuple[type[BaseException], ...]


async def _try_call(io: CryosleepIO, fn: Callable[[], Awaitable[object]]) -> tuple[object, Exception | None]:
    assert io is not None and callable(fn)
    try:
        return await fn(), None
    except io.failures as exc:
        return None, exc


async def _displace_quiet(state: CryosleepState, io: CryosleepIO, quiet_settle_s: float) -> None:
    assert state is not None and quiet_settle_s >= 0
    qm, _ = await _try_call(io, io.get_quiet)
    if isinstance(qm, dict) and qm.get("enabled"):
        await _try_call(io, lambda: io.set_quiet(False))
        if quiet_settle_s > 0:
            await io.sleep(quiet_settle_s)
    state.holding_reserve = False


async def _apply_cryo_mute(state: CryosleepState, io: CryosleepIO) -> Exception | None:
    assert state is not None and io is not None
    cm, merr = await _try_call(io, lambda: io.set_cryo_mute(True))
    state.cryo_muted = bool(isinstance(cm, dict) and cm.get("muted"))
    return merr


def _resolve_asleep(rstate: object, known_asleep: bool | None) -> bool | None:
    if isinstance(rstate, dict) and isinstance(rstate.get("asleep"), bool):
        return rstate["asleep"]
    return known_asleep if isinstance(known_asleep, bool) else None


async def _apply_entry_sleep(state: CryosleepState, io: CryosleepIO, asleep: bool | None) -> None:
    assert state is not None and io is not None
    if asleep is True:
        state.was_asleep, state.awake_polls = True, 0
        return
    state.was_asleep, state.awake_polls, state.last_sleep_at = False, 0, io.now()
    if not state.cryo_muted and asleep is False:
        _, serr = await _try_call(io, io.send_sleep)
        state.last_error = str(serr)[:200] if serr is not None else ""


async def execute_enter(state: CryosleepState, io: CryosleepIO, cancelled: Callable[[], bool], *,
                        quiet_settle_s: float = QUIET_SETTLE_S, known_asleep: bool | None = None) -> None:
    """Displace quiet mode, engage runtime mute/force-sleep, snapshot screen, and zero backlight."""
    assert state is not None and io is not None
    await _displace_quiet(state, io, quiet_settle_s)
    if cancelled():
        return
    merr = await _apply_cryo_mute(state, io)
    if cancelled():
        return
    rstate, rerr = await _try_call(io, io.get_robot_state)
    if cancelled():
        return
    if rerr is not None and merr is not None:
        state.phase, state.applied = PHASE_WAITING_ROBOT, False
        state.last_error = f"robot unreachable; cryosleep armed ({merr})"[:200]
        return
    asleep = _resolve_asleep(rstate, known_asleep)
    if not state.snapshot_taken:
        bl, _ = await _try_call(io, io.get_backlight)
        state.record_snapshot(bl if isinstance(bl, int) and not isinstance(bl, bool) else None)
    await _apply_entry_sleep(state, io, asleep)
    await _try_call(io, lambda: io.set_backlight(0))
    state.applied, state.last_guard_at = True, io.now()
    state.phase = PHASE_HOLDING_STILL if state.holding_reserve else PHASE_ACTIVE


async def _run_drift_guard(state: CryosleepState, io: CryosleepIO) -> None:
    assert state is not None and io is not None
    cm, _ = await _try_call(io, io.get_cryo_mute)
    if isinstance(cm, dict) and not cm.get("muted"):
        cm, _ = await _try_call(io, lambda: io.set_cryo_mute(True))
    if isinstance(cm, dict):
        state.cryo_muted = bool(cm.get("muted"))
    bl, _ = await _try_call(io, io.get_backlight)
    if isinstance(bl, int) and bl != 0:
        await _try_call(io, lambda: io.set_backlight(0))


async def execute_poll_action(action: str, state: CryosleepState, io: CryosleepIO, cancelled: Callable[[], bool], *,
                              quiet_settle_s: float = QUIET_SETTLE_S) -> None:
    """Carry out one poll-cycle decision while Cryosleep is armed."""
    assert action and state is not None
    if action == POLL_APPLY:
        await execute_enter(state, io, cancelled, quiet_settle_s=quiet_settle_s, known_asleep=None)
    elif action == POLL_SETTLED_ASLEEP:
        if state.holding_reserve:
            await _try_call(io, lambda: io.set_quiet(False))
            state.holding_reserve = False
            await _apply_cryo_mute(state, io)
        await _try_call(io, lambda: io.set_backlight(0))
    elif action == POLL_REENFORCE_SLEEP:
        if state.holding_reserve:
            await _displace_quiet(state, io, quiet_settle_s)
        await _apply_cryo_mute(state, io)
        if not state.cryo_muted:
            _, serr = await _try_call(io, io.send_sleep)
            if serr is not None:
                state.last_error = str(serr)[:200]
        await _try_call(io, lambda: io.set_backlight(0))
    elif action == POLL_LOCK_STILL:
        await _try_call(io, lambda: io.set_quiet(True))
        await _apply_cryo_mute(state, io)
        await _try_call(io, lambda: io.set_backlight(0))
    elif action == POLL_DRIFT_GUARD:
        await _run_drift_guard(state, io)


async def execute_exit(state: CryosleepState, io: CryosleepIO, cancelled: Callable[[], bool], *,
                       skip_backlight: Callable[[], bool] | None = None,
                       known_asleep: bool | None = None) -> bool | None:
    """Release Cryosleep mute, restore backlight, and return saved camera_allowed (None if cancelled)."""
    assert state is not None and io is not None
    if state.holding_reserve:
        await _try_call(io, lambda: io.set_quiet(False))
        state.holding_reserve = False
    if cancelled():
        return None
    await _try_call(io, lambda: io.set_cryo_mute(False))
    if cancelled():
        return None
    if state.snapshot_taken and not (callable(skip_backlight) and skip_backlight()):
        rstate, _ = await _try_call(io, io.get_robot_state)
        asleep = _resolve_asleep(rstate, known_asleep)
        target_bl = state.target_restore_backlight(asleep if isinstance(asleep, bool) else None)
        if not (cancelled() or (callable(skip_backlight) and skip_backlight())):
            await _try_call(io, lambda: io.set_backlight(target_bl))
    return state.finish_exit()

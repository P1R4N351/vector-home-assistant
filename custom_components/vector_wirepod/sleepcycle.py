"""Sleep cycle for one Vector. No Home Assistant imports, so it is testable alone.

What it does, in the owner's words (2026-11-19): "sleep gets to run until bot is woken, waking
changes wake state and triggers quiet mode on a timer followed by return to triggered sleep".

    idle ──request_sleep──▶ going_to_sleep ──robot reports asleep──▶ sleeping
                                 ▲                                      │ robot reports awake
                                 │ quiet_hold_s elapsed                 ▼
                                 └────────────────────────────── woken_quiet

Quiet mode holds the robot still and that stops it going to sleep, so quiet mode is switched OFF
whenever a sleep is requested and only switched ON after the robot has been woken. If the robot
falls asleep by itself while quiet mode is held, nothing is changed and the cycle waits for the
next wake. The cycle never decides
anything from an unknown reading: observe(None, ...) changes nothing.

This file only decides. It returns the actions to carry out; the caller performs them.
"""

from __future__ import annotations

from dataclasses import dataclass

IDLE = "idle"
GOING = "going_to_sleep"
SLEEPING = "sleeping"
WOKEN = "woken_quiet"
PHASES = (IDLE, GOING, SLEEPING, WOKEN)

ACT_QUIET_ON = "quiet_on"
ACT_QUIET_OFF = "quiet_off"
ACT_SLEEP = "sleep"
ACT_PHOTO = "photo"


@dataclass
class SleepCycle:
    quiet_hold_s: float = 600.0   # how long quiet mode is held after a wake before sleep is asked again
    settle_s: float = 180.0       # how long a sleep request may take before it counts as not taken
    photo_on_wake: bool = False
    phase: str = IDLE
    since: float = 0.0            # when the current phase began (caller's clock)
    wakes: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        assert self.quiet_hold_s > 0 and self.settle_s > 0
        assert self.phase in PHASES

    def _enter(self, phase: str, now: float, note: str = "") -> None:
        assert phase in PHASES
        self.phase, self.since, self.note = phase, now, note

    def request_sleep(self, now: float) -> list[str]:
        """The owner asked for sleep (button or automation)."""
        self._enter(GOING, now, "sleep requested")
        return [ACT_QUIET_OFF, ACT_SLEEP]

    def cancel(self, now: float, why: str) -> list[str]:
        """Stop managing sleep, for example when quiet mode or docking is set by hand."""
        if self.phase != IDLE:
            self._enter(IDLE, now, why)
        return []

    def restore(self, phase: str, since: float, wakes: int) -> bool:
        """Take up a cycle that was running before a restart. False when the values are not usable."""
        if phase not in PHASES or phase == IDLE or self.phase != IDLE:
            return False
        if isinstance(since, bool) or not isinstance(since, (int, float)) or since <= 0:
            return False
        self._enter(phase, float(since), "resumed after restart")
        self.wakes = wakes if isinstance(wakes, int) and not isinstance(wakes, bool) and wakes >= 0 else 0
        return True

    def quiet_seconds_left(self, now: float) -> float | None:
        if self.phase != WOKEN:
            return None
        return max(0.0, self.quiet_hold_s - (now - self.since))

    def observe(self, asleep: bool | None, now: float) -> list[str]:
        """Feed one reading of the robot's own asleep flag. Returns the actions to carry out."""
        if asleep is None or self.phase == IDLE:
            return []
        if self.phase == GOING:
            if asleep:
                self._enter(SLEEPING, now, "asleep")
            elif now - self.since > self.settle_s:
                self._enter(IDLE, now, "the robot did not go to sleep")
            return []
        if self.phase == SLEEPING:
            if asleep:
                return []
            self.wakes += 1
            self._enter(WOKEN, now, "woken")
            return ([ACT_PHOTO] if self.photo_on_wake else []) + [ACT_QUIET_ON]
        # WOKEN: quiet mode is being held on a timer.
        if asleep:
            # Leave quiet mode as it is: changing control of a sleeping robot can wake it.
            self._enter(SLEEPING, now, "fell asleep again by itself")
            return []
        if now - self.since >= self.quiet_hold_s:
            self._enter(GOING, now, "quiet time over; back to sleep")
            return [ACT_QUIET_OFF, ACT_SLEEP]
        return []

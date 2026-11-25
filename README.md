# Vector for Home Assistant

A Home Assistant custom integration for Vector robots (Anki, later Digital Dream Labs) that are
served by [wire-pod](https://github.com/kercre123/wire-pod). One config entry per robot. Domain
`vector_wirepod`.

Not affiliated with Anki or Digital Dream Labs.

## What each robot gets

**Camera and photos**

| Entity | Notes |
|---|---|
| Camera | Live view and thumbnails in the robot's own frame shape (16:9 on Vector 1.0, 4:3 on 2.0). One wire-pod stream is shared by every viewer and closed when nobody watches. A sleeping robot sends no frames; the camera then shows its status instead of waking it. |
| Camera allowed (switch) | Off closes the stream and keeps it closed, for runs where something else needs the camera. |
| Take photo (button), Photo on wake (switch), Last photo (image) | Photos are kept in `www/vector_wirepod/<serial>/` (newest 50); each fires the event `vector_wirepod_photo`. |

**Status**

| Entity | Notes |
|---|---|
| Battery voltage, Battery level (sensors); On charger, Charging, Robot reachable (binary sensors) | Polled every 60 s by default. |
| Asleep (binary sensor) | The robot's own flag. |
| Picked up, Being held, Back button pressed, Moving, Animating, Edge detected (binary sensors) | The robot's own status flags, from the same reading as Asleep. Unknown, not off, when the robot cannot be read. |
| Camera status, Sleep cycle (sensors) | Diagnostic. |

**Robot data** (read every 15 minutes; reading changes nothing on the robot and does not wake it)

| Entity | Source |
|---|---|
| Time switched on, Time petted (sensors) | Lifetime counters `Alive.seconds` and `Pet.ms`. Time switched on is powered time, not age. |
| Distance driven (sensor) | `Odom.Body`, stored by the robot as millimetres × 100000. |
| Wake word reactions (sensor) | `BStat.ReactedToTriggerWord`. |
| Lifetime counters (diagnostic sensor) | Every lifetime counter, unchanged, as attributes. |
| Locale, Time zone, Location, IP address (diagnostic sensors) | The robot's settings, and its address as wire-pod lists it. The robot's SDK token is never exposed. |

Units are from the robot's own source (`engine/components/robotStatsTracker.cpp` in
[wire-os-victor](https://github.com/os-vector/wire-os-victor)).

**Actions and settings**

| Entity | Notes |
|---|---|
| Dock, Sleep, Go to sleep, Explore, Listen for beat, Fetch cube, Trigger wake word (buttons) | Fixed wire-pod intents. Sleep starts a repeating cycle: sleep until woken, quiet mode for a set time (default 600 s), then sleep again. |
| Quiet mode (switch) | Holds the robot still and quiet. |
| Volume (number) | Master volume, 0 (mute) to 5 (high). |
| Eye colour (select) | The seven preset colours, or Custom when the robot has a custom colour, which choosing it restores. Updates availability immediately when Cryosleep toggles. |
| Face backlight (light), Mute microphones (switch) | Need wire-pod to log in to the robot (developer robots). Changing the microphone mute restarts the robot's programs (about 20 s). |
| Cryosleep (switch) | For overnight automations: turns off quiet mode and the controls above, mutes voice requests and the speaker without a restart, puts the robot to sleep, turns the face backlight off, keeps it asleep, and restores everything when switched off. |

**Manual controls**

| Entity | Notes |
|---|---|
| Drive forward, Drive backward, Turn left, Turn right (buttons) | One step each: 100 mm forward, 50 mm back, or 45° on the spot. |
| Stop (button) | Stops the motors. Available whenever the robot is reachable. |
| Head angle (−22° to 45°), Lift height (0 to 100 %) (numbers) | Show the last value set; wire-pod does not report either back. |
| Say (text) | The robot says the text, up to 255 characters. Temporarily lifts Quiet mode while speaking and restores it afterwards (blocked during Cryosleep); clears input after sending and records `last_said` in attributes. |
| Manual control (switch) | On while Home Assistant holds control of the robot. Off releases it at once. |

How the manual controls behave:

- Control is requested at DEFAULT priority, never `OVERRIDE_BEHAVIORS`, so the robot still reacts
  to edges, falls and being picked up while it is driven.
- Only fixed endpoints with range-checked arguments are sent.
- The first command takes control and ends any running sleep cycle. Control is released 60 s
  after the last command, because holding it keeps the robot awake.
- Manual motion controls are refused during Quiet mode and Cryosleep (except Stop, which always runs;
  and Say, which temporarily lifts Quiet mode while blocked during Cryosleep). Turning Cryosleep on
  releases control.
- Refusals from wire-pod (on the charger, an edge ahead, something in the way, being held) are
  shown as errors in plain words.

**Outage resilience and failover (`0.13.0–0.13.1`)**

- Non-blocking startup (`async_refresh()`) lets entities register cleanly even when wire-pod is
  unreachable at Home Assistant boot.
- Optional `fallback_url` in setup, reconfigure, and options fails over automatically on HTTP `502`/`504`,
  connection errors, and timeouts (`5.0 s` connect timeout), re-probing the primary server every `120 s`.
  The camera exposes `active_pod_url`.
- Event-loop `@callback` recovery handlers refresh polling entities (`Face backlight`, `Volume`,
  `Mute microphones`, `Quiet mode`, robot data) immediately on reconnect, and `Quiet mode` preserves
  its `desired` attribute across `unavailable` periods.

## Requirements

- Home Assistant. Tested with 2026.11.3.
- A wire-pod server that the robot is paired with, reachable from Home Assistant over HTTP.

Stock wire-pod provides the camera, battery, intents, wake word, volume, robot data, eye colour,
speech and behavior control. The entities below call endpoints that stock wire-pod does not
have; on a stock server they are unavailable or report an error.

| Endpoint | Used by |
|---|---|
| `/api-sdk/robot_state` | Asleep, the status flags, the sleep cycle |
| `/api-sdk/quiet_mode` | Quiet mode, the sleep cycle |
| `/api-sdk/backlight`, `/api-sdk/mic_mute` | Face backlight, Mute microphones |
| `/api-sdk/cryo_mute` | Cryosleep |
| `/api-sdk/drive_straight`, `turn_in_place`, `set_head_angle`, `set_lift_height`, `stop_motors` | The manual controls |

On stock wire-pod a robot reflex (an edge, a fall) ends the control hold for good: turn
Manual control off and on to take it back.
[kercre123/wire-pod#550](https://github.com/kercre123/wire-pod/pull/550) proposes asking again
automatically.

## Install

- **HACS:** add this repository as a custom repository (type Integration), install **Vector**,
  restart Home Assistant.
- **By hand:** copy `custom_components/vector_wirepod/` (with its `brand/` folder) into the
  `custom_components/` folder of your configuration, then restart.
- **Supervised host over SSH:** `./deploy.sh --host <host> --restart`.

Then add the integration under Settings → Devices & services, enter the wire-pod address
(default `http://escapepod.local:8080`), and choose the robot. A sleeping robot can be added; its
entities fill in as it answers.

## Example: Cryosleep overnight

```yaml
automation:
  - alias: "Vector: overnight Cryosleep"
    triggers:
      - trigger: time
        at: "23:00:00"
        id: sleep
      - trigger: time
        at: "08:00:00"
        id: wake
    actions:
      - action: >
          {{ "switch.turn_on" if trigger.id == "sleep" else "switch.turn_off" }}
        target:
          entity_id: switch.vector_demo_cryosleep
```

## Known limits

- A wake is noticed at the next poll: up to 60 s late, or 20 s while a sleep cycle runs.
- Under quiet mode the camera sends no frames, so a photo lifts quiet mode for the few seconds it
  takes.
- wire-pod answers `say_text` with "success" whether or not the robot spoke.
- Holding control wakes a sleeping robot within about a second. Control is shared with
  wire-pod's own web control page: releasing it from Home Assistant also ends a hold taken there.
- The first command after control is taken waits 1.5 s, because wire-pod answers before the robot
  has granted control.
- Listening through the microphones is not offered. The SDK audio feed in current robot firmware
  sends a 1 kHz test tone, not microphone audio (`engine/components/sdkComponent.cpp`).

## Tests

```sh
tests/check.sh          # unit tests, then the mutation suite
tests/check.sh --quick  # unit tests only
```

The tests run with aiohttp and Pillow; Home Assistant is used when installed and replaced by
stand-ins otherwise. `tests/mutate.py` breaks one rule at a time and checks that a test fails.

## Licence

Code: Mozilla Public License 2.0 (`LICENSE`). Documentation: CC BY 4.0. The icon in
`custom_components/vector_wirepod/brand/` is not covered by either; see `NOTICE`.

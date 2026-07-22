# FreeSolo scale, grinder, and hot-water tools

xBloom Studio exposes its scale, grinder, and brewer independently. The official app calls this
FreeSolo mode. These commands are separate from coffee/tea recipe loading and must not be used
while a recipe state record exists.

## Electronic scale

Entering the official FreeSolo scale mode sends command `8003`. On tested firmware this command
automatically zeros whatever load is already on the platform. This is separate from the explicit
`8500` tare button command, but there is no decoded alternative that enters scale mode while
preserving the pre-entry absolute load.

To weigh an object's absolute mass, start with the platform empty:

```text
python scripts/xbloom.py scale --duration 30
```

Wait for the JSON `"status": "ready"` event, then place the object. To weigh net contents instead,
put the empty cup or vessel on the platform before running the command; entry will make that vessel
the zero baseline, so add the contents only after `ready`.

Send an additional explicit tare only when the user asks to re-zero the entry baseline:

```text
python scripts/xbloom.py scale --tare --duration 30
```

`--tare` sends `8500` after the mandatory entry auto-zero; omitting it is not a true
"enter without tare" mode. The command emits timestamped gram readings and sends scale-exit from a
`finally` block. The timer is maintained by the client/Agent; the Android app also implements its
scale timer locally rather than with a machine command. Maximum rated load is 2 kg.

Hardware status: enter, live readings, and clean exit were verified on firmware `V12.0D.500` on
2026-07-12. A follow-up test with a cup already present confirmed the entry baseline: it read 0,
removing the cup produced the corresponding negative value, and replacing it returned to 0.

### Interactive absolute-weight workflow

1. Ask the user to clear the platform. Do not enter scale mode with the object already present.
2. Run `scale` without `--tare` and wait for `"status": "ready"` with
   `"baseline_zeroed": true`. Use `--duration 60` to `90` when a chat round trip is expected.
3. Surface `ready` immediately and ask the user to place the object centered, without touching the
   chassis. Do not wait for the scale session to exit before giving this instruction.
4. Report a result only after several samples agree on a plausible positive weight. For a cup,
   an all-zero stream is not a 0 g cup; it means the object was zeroed at entry or was never placed.
5. If no stable positive reading arrives, exit normally, explain that no absolute weight was
   measured, and retry only after the user reconfirms an empty platform.

Supported standalone current-weight reports preserve negative values when a tared load is removed.
Use those readings to explain the zero baseline; do not turn a negative reading into an absolute
object weight.

### BLE contention

Treat Studio BLE as single-controller in practice. An open/connected phone xBloom App can make
Agent scans find no machine or cause a FreeSolo session to time out. Before BLE work, have the user
fully close or disconnect the App. After an uncertain physical action, keep the vessel in place,
use `cancel` or the machine's physical control, and confirm the machine is idle before retrying.

For interactive work, `bridge start` creates a loopback-only daemon without connecting; `bridge
connect` then holds one app-style session. The bridge is the sole BLE owner: top-level active
commands (including scale, tea, grinder, water, presets, settings, and tuning) use typed bridge RPC
through the daemon; only passive `scan` / `doctor --scan` discover BLE directly. Use
`bridge events` or top-level `monitor` (status/events observation only) rather than a competing
client-side notification subscription.

For an interactive scale session:

```text
python scripts/xbloom.py bridge scale-start --duration 90
python scripts/xbloom.py bridge scale-tare
python scripts/xbloom.py bridge cancel
```

`scale-start` returns without blocking the local Agent call; readings appear in bridge status and
events. Entry still performs mandatory auto-zero. `scale-tare` is an additional `8500` write and
is accepted only while that bridge-owned scale session is running.

## Standalone grinder

Official limits are setting 1-80, 60-120 RPM, no more than 30 seconds per run, followed by at least
60 seconds of rest. The wrapper persists a conservative runtime-plus-rest lock so a new Agent
process cannot immediately start another cycle.

This physical action requires both an owner opt-in and a current readiness confirmation:

```text
XBLOOM_ENABLE_REMOTE_GRINDER=I_UNDERSTAND_REMOTE_GRINDER
python scripts/xbloom.py grind --size 62 --rpm 100 --seconds 10 \
  --confirm-ready beans-cup-clear
```

Before running, confirm beans are in the grinder, the receiving cup is centered below the chute,
the chute is unobstructed, and hands/objects are clear. The client always attempts STOP and QUIT
from `finally`, including on Ctrl+C or an ordinary task cancellation; a normal successful result
also requires the STOP acknowledgement. Do not bypass the 60-second rest lock.

Hardware status: command layout, time limit, cleanup order, cancellation cleanup, and rest lock are
covered by deterministic tests against the official Android implementation. A physical grinder run
is intentionally not part of unattended release testing.

The persistent form keeps the same owner/readiness gates and rest lock while allowing the timer to
pause and resume:

```text
python scripts/xbloom.py bridge start
python scripts/xbloom.py bridge grinder-start --size 62 --rpm 100 --seconds 10 \
  --confirm-ready beans-cup-clear
python scripts/xbloom.py bridge pause
python scripts/xbloom.py bridge resume
python scripts/xbloom.py bridge cancel
```

The bridge owns STOP/QUIT and writes an `in_progress` rest record before starting. If its process
dies, that record blocks an immediate restart instead of assuming the grinder stopped safely.

## Temperature/volume water

The guarded FreeSolo brewer range is narrower than the app's published 500 ml maximum:

- 20-360 ml.
- `RT` room-temperature/pass-through mode, or numeric 40-98 C.
- 3.0-3.5 ml/s in 0.1 steps.
- `center`, `spiral`, or `circular` pattern (`ring` is a legacy alias).
- `tank` or `tap` water source (`tap` is the compatibility name for direct feed/auto refill).

This physical hot-water action uses the same deployment opt-in as coffee/tea remote start, plus its
own readiness phrase:

```text
XBLOOM_ENABLE_REMOTE_START=I_UNDERSTAND_REMOTE_HOT_WATER
python scripts/xbloom.py water --volume 250 --temp 85 --flow 3.5 --pattern center \
  --water-source auto --confirm-ready vessel-water-clear
```

For unheated room-temperature water, use the explicit app token:

```text
python scripts/xbloom.py water --volume 250 --temp RT --flow 3.5 --pattern center \
  --water-source auto --confirm-ready vessel-water-clear
```

The Android app maps RT to the J15 constant `20.0` and sends `200.0` in the temperature float field.
This selects pass-through/unheated water; Studio does not cool warmer source water to exactly 20 C.
The water-action owner gate and readiness confirmation still apply because the machine physically
dispenses water even when heating is off.

The app includes the current J15 water source in machine info and passes it into command `4506`.
`--water-source auto` mirrors that behavior. If the setting cannot be decoded, the wrapper stops
and asks for `--water-source tank` or `--water-source tap`; it never silently guesses a path. This
selects the source for the bounded dispense and does not rewrite the machine's persistent setting.

Confirm the selected source is actually available (filled tank or live direct feed), a sufficiently
large heat-safe vessel is centered below the spout, the brewer/dripper path is appropriate for the
intended action, and people/objects are clear.
The machine meters the requested volume. The wrapper retains both the latest report and the
session peak because firmware can reset the current value to zero after STOP. It accepts natural
completion only when the peak is within a small tolerance of the target; an early stop, missing
meter value, timeout, or interruption triggers STOP/QUIT cleanup and a failure instead of a false
completion claim.

Keep the water quantities distinct in CLI/bridge output:

- `target_dispensed_water_ml` is the programmed target for this operation.
- `dispensed_water_ml` is cumulative output reported by machine command `40523`; it is not supply
  level or capacity.
- `cup_weight_g` is the raw platform reading, while `cup_delta_g` is the observed increase from
  the operation baseline. It may be lower than machine output because water can remain in coffee,
  a filter, dripper, or tea accessory.

The protocol exposes the chosen tank/direct-feed path and a water-available flag, not quantitative
supply inventory.

Hardware status: frames and completion/timeout cleanup are tested against a scripted BLE device and
the command semantics are decoded from the official app. A real hot-water dispense is never run as
an unattended self-test.

For RT water, successful JSON must report `"status": "complete"`, `"temp_setting": "RT"`, and a
metered volume near the request. Top-level `water --timeout` is an **observation bound only**
(poll the returned `workflow_id` until terminal or bound); client exit or observation timeout never
cancels, releases, or mutates the daemon workflow. If observation ends without a confirmed
terminal, keep the vessel centered, confirm the phone App is disconnected, use explicit `cancel`
only when the machine may still be active, then re-observe with `monitor --workflow-id` rather than
blindly restarting. Do not silently substitute 40 C for RT; that changes the requested drink and
requires the user's acceptance.

### Interactive water and live targets

The bridge holds the FreeSolo brewer session so pause/resume and event polling are state-safe:

```text
python scripts/xbloom.py bridge start
python scripts/xbloom.py bridge water-start --volume 250 --temp 85 --flow 3.5 \
  --pattern center --water-source auto --confirm-ready vessel-water-clear
python scripts/xbloom.py bridge pause
python scripts/xbloom.py bridge resume
python scripts/xbloom.py bridge events --since 0
python scripts/xbloom.py bridge cancel
```

It also starts a conservative host-side safety timer. Natural report `40511` is recorded as
`complete` only when that session's peak metered volume is within tolerance of the requested
target; missing or early volume becomes `completion_unconfirmed`. If the terminal report never
arrives, the timer attempts STOP/QUIT and records `safety_timeout_stopped` rather than claiming a
completed dispense. An explicit STOP is confirmed by echo `4507`, not by waiting for `40511`; quit
`8013` then leaves FreeSolo mode.

The APK also sends command `4510` to change the temperature target and `8016` to change the pattern
during a running or paused FreeSolo session. This does **not** mean arbitrary recipe editing:

- Temperature affects only water not yet delivered, with heater/plumbing lag; it is not an instant
  outlet-temperature guarantee. `RT` remains pass-through rather than active cooling.
- Pattern changes the remaining `center`/`spiral`/`circular` outlet motion.
- The start command fixes total volume and flow. This bridge does not change either mid-session.
- These commands do not alter coffee-recipe pours, bypass, grind, or pause fields.

Their protocol frames are deterministic-test covered. A supervised run on 2026-07-13 physically
verified a running `center → spiral` change on firmware `V12.0D.500`. Live-temperature command
`4510` encoding and the completed BLE write path are verified, but no thermometer measurement has
yet established the physical outlet response; paused-state behavior also remains unverified. Both controls still require the separate
`XBLOOM_ENABLE_LIVE_ADJUST=I_ACCEPT_UNVERIFIED_LIVE_ADJUST` owner gate and the same exact
`--confirm-live-adjust` value on each call:

```text
python scripts/xbloom.py bridge water-temperature --temp 60 \
  --confirm-live-adjust I_ACCEPT_UNVERIFIED_LIVE_ADJUST
python scripts/xbloom.py bridge water-pattern --pattern spiral \
  --confirm-live-adjust I_ACCEPT_UNVERIFIED_LIVE_ADJUST
```

For `water-pattern`, `hardware_effect_verified` is true only on a firmware with recorded hardware
evidence; `report_observed` separately states whether optional report `8107` arrived. Firmware
`V12.0D.500` applied command `8016` during the hardware run without echoing it, so absence of that
optional report is not treated as a failed BLE write. `water-temperature` reports
`command_write_verified: true`, `outlet_temperature_effect_measured: false`, and the legacy
`hardware_effect_verified: false`; optional report `8108` likewise is not physical outlet-temperature proof.

The verified run used a 200 ml RT target and was fail-safe stopped near 100 ml after the visible
pattern change. Echo `4507`, quit `8013`, and the idle state confirmed explicit STOP cleanup; the
run does not prove natural target-volume completion. Restart an idle bridge after changing the
owner gate because the daemon captures its environment at launch. Use a thermometer, heat-safe
vessel, and current in-person confirmation for the first temperature A/B; never make live
adjustment part of an unattended test.

## Evidence boundary

- Published limits and FreeSolo behavior: official xBloom documentation.
- Command identifiers, RT sentinel, and argument encoding: interoperability analysis of the
  official Android app.
- 360 ml cap, explicit environment gates, readiness phrases, and persisted rest lock: this Skill's
  deliberately stricter safety policy.
- Coffee/grinder/water pause-resume and FreeSolo live-target frames: deterministic protocol and
  scripted-client tests. Running pattern change and explicit FreeSolo STOP are hardware-verified on
  `V12.0D.500`; live-temperature write correctness is verified while its measured outlet response
  and paused-state adjustment remain A/B pending.

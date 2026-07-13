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

## Temperature/volume water

The guarded FreeSolo brewer range is narrower than the app's published 500 ml maximum:

- 20-360 ml.
- `RT` room-temperature/pass-through mode, or numeric 40-98 C.
- 3.0-3.5 ml/s in 0.1 steps.
- `center`, `spiral`, or `ring` pattern.
- `tank` or `tap` water source.

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
This selects pass-through/unheated water; Studio does not cool warmer tank water to exactly 20 C.
The water-action owner gate and readiness confirmation still apply because the machine physically
dispenses water even when heating is off.

The app includes the current J15 water source in machine info and passes it into command `4506`.
`--water-source auto` mirrors that behavior. If the setting cannot be decoded, the wrapper stops
and asks for `--water-source tank` or `--water-source tap`; it never silently guesses a path. This
selects the source for the bounded dispense and does not rewrite the machine's persistent setting.

Confirm the selected source is actually available (filled tank or live tap feed), a sufficiently
large heat-safe vessel is centered below the spout, the brewer/dripper path is appropriate for the
intended action, and people/objects are clear.
The machine meters the requested volume. The wrapper retains the latest water-volume report and
accepts the firmware stop only when it is within a small tolerance of the target; an early stop,
missing meter value, timeout, or interruption triggers STOP/QUIT cleanup and a failure instead of a
false completion claim.

Hardware status: frames and completion/timeout cleanup are tested against a scripted BLE device and
the command semantics are decoded from the official app. A real hot-water dispense is never run as
an unattended self-test.

For RT water, successful JSON must report `"status": "complete"`, `"temp_setting": "RT"`, and a
metered volume near the request. A timeout or interruption is not success: keep the vessel centered,
confirm the phone App is disconnected, use `cancel` if the machine may still be active, then retry
at most once with a finite `--timeout` and the source reported by machine preflight. Do not silently
substitute 40 C for RT; that changes the requested drink and requires the user's acceptance.

## Evidence boundary

- Published limits and FreeSolo behavior: official xBloom documentation.
- Command identifiers, RT sentinel, and argument encoding: interoperability analysis of the
  official Android app.
- 360 ml cap, explicit environment gates, readiness phrases, and persisted rest lock: this Skill's
  deliberately stricter safety policy.

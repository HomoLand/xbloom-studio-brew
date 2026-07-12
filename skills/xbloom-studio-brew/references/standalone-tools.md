# FreeSolo scale, grinder, and hot-water tools

xBloom Studio exposes its scale, grinder, and brewer independently. The official app calls this
FreeSolo mode. These commands are separate from coffee/tea recipe loading and must not be used
while a recipe state record exists.

## Electronic scale

Read without changing tare:

```text
python scripts/xbloom.py scale --duration 30
```

Tare only when the user explicitly asks and the intended empty vessel is already in place:

```text
python scripts/xbloom.py scale --tare --duration 30
```

The command enters scale mode, emits timestamped gram readings, and sends scale-exit from a
`finally` block. The timer is maintained by the client/Agent; the Android app also implements its
scale timer locally rather than with a machine command. Maximum rated load is 2 kg.

Hardware status: enter, live 0.0 g readings, and clean exit were verified on firmware
`V12.0D.500` on 2026-07-12. Tare was intentionally not changed during that check.

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
- 40-98 C, numeric temperatures only.
- 3.0-3.5 ml/s in 0.1 steps.
- `center`, `spiral`, or `ring` pattern.

This physical hot-water action uses the same deployment opt-in as coffee/tea remote start, plus its
own readiness phrase:

```text
XBLOOM_ENABLE_REMOTE_START=I_UNDERSTAND_REMOTE_HOT_WATER
python scripts/xbloom.py water --volume 250 --temp 85 --flow 3.5 --pattern center \
  --confirm-ready vessel-water-clear
```

Confirm the tank contains water, a sufficiently large heat-safe vessel is centered below the
spout, the brewer/dripper path is appropriate for the intended action, and people/objects are clear.
The machine meters the requested volume. The wrapper retains the latest water-volume report and
accepts the firmware stop only when it is within a small tolerance of the target; an early stop,
missing meter value, timeout, or interruption triggers STOP/QUIT cleanup and a failure instead of a
false completion claim.

Hardware status: frames and completion/timeout cleanup are tested against a scripted BLE device and
the command semantics are decoded from the official app. A real hot-water dispense is never run as
an unattended self-test.

## Evidence boundary

- Published limits and FreeSolo behavior: official xBloom documentation.
- Command identifiers/argument encoding: interoperability analysis of the official Android app.
- 360 ml cap, explicit environment gates, readiness phrases, and persisted rest lock: this Skill's
  deliberately stricter safety policy.

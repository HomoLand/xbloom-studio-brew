# BLE and hot-water safety

The BLE protocol is unofficial and reverse-engineered. It is not an xBloom API. The vendored
implementation has been tested by its upstream project only on firmware `V12.0D.500`; this
skill also completed its initial hardware validation on that firmware.

## Command risk matrix

| Command | Machine effect | Brew-control opcode |
| --- | --- | --- |
| `doctor` | Local dependency check; optional passive scan. | None |
| `scan` | Discovers nearby advertisements. | None |
| `probe` | Opens an app-style session and asks for status, firmware, and redacted machine settings. | None (`0xA4`, `0x56`) |
| `settings` / `advanced` | Reads persistent user settings or APK-defined mechanical tuning. | None; read-only |
| `set-settings` | Persistently changes units, display brightness, or water source, then reads back. | Owner + per-write gates; no brew command |
| `set-advanced` | Persistently changes radius/amplitude levels, then reads back. | Owner + per-write gates; no brew command |
| `validate` | Parses and validates a local recipe. | None |
| `load` | Writes guarded recipe frames and leaves the machine armed. Does not brew. | None |
| `tea-validate` | Parses a local Omni Tea Brewer recipe. | None |
| `tea-load` | Uploads tea cup geometry and recipe data; does not execute it. | None |
| `monitor` | Subscribes to state and scale notifications. | None |
| `scale` | Enters the electronic-scale screen, auto-zeros the entry load, optionally re-tares, streams grams, then exits. | No motor/water command |
| `save-slots` | Persistently overwrites all three on-machine A/B/C presets. Does not brew. | None |
| `cancel` | Cancels/exits an armed or active operation. | `0x47` cancel |
| `start` | Commits and starts an armed recipe; can grind and dispense near-boiling water. | `0x42`, sometimes `0x46` |
| `grind` | Runs the standalone grinder for a bounded interval. | Motor command; owner + per-run gates |
| `water` | Dispenses a requested volume at a requested temperature from tank/tap. | Hot-water command; owner + per-run gates |
| `tea-start` | Executes a loaded siphon-tea recipe. | Hot-water command; owner + per-run gates |
| `tea-brew` | Loads and explicitly executes tea on one connection, then monitors. | Same hot-water gates as `tea-start` |
| `bridge start/status/events` | Starts or queries a loopback daemon; starting it does not connect or actuate. | None |
| `bridge connect/disconnect` | Holds or releases one app-style BLE session. | None (`0xA4`, `0x56`) |
| `bridge coffee-load/coffee-start` | Loads, then explicitly starts coffee through one held connection. | Same load/start gates as one-shot coffee |
| `bridge tea-load/tea-start` | Loads, then explicitly starts tea through one held connection. | Same load/start gates as one-shot tea |
| `bridge scale-start/scale-tare` | Runs non-blocking scale mode and optional explicit re-tare. | No motor/water command; entry still auto-zeros |
| `bridge settings/advanced` | Reads persistent settings/tuning through the held connection. | Read-only |
| `bridge set-settings/set-advanced` | Writes with readback and rollback through the held connection. | Same persistent-write gates as one-shot settings |
| `bridge save-slots` | Atomically replaces A/B/C without brewing. | Same explicit overwrite intent as one-shot presets |
| `bridge grinder-start/water-start` | Starts a bounded interactive FreeSolo session. | Same motor/hot-water gates as one-shot tools |
| `bridge pause/resume/cancel` | Controls only the activity currently owned by the bridge. | State-sensitive activity command |
| `bridge water-pattern` | Changes a running/paused FreeSolo water pattern target. | Separately gated; running `center → spiral` verified on `V12.0D.500` |
| `bridge water-temperature` | Changes a running/paused FreeSolo water temperature target. | Separately gated; hardware A/B pending |

## Mandatory operating rules

1. Do not touch BLE when the user only asked for a recipe. Generate and validate the file.
2. Use `scripts/xbloom.py`; never invoke the vendored client or raw protocol builders directly.
3. Run `probe` only before loading, never while an armed-state record exists.
4. Validate before load. Treat validation errors as blockers; do not weaken limits ad hoc.
5. Load is the default device action. It arms the recipe and lets the user approve physically.
6. Use `start`, `water`, or `tea-start` only when the deployment owner enabled hot-water actions
   and the user explicitly confirms the command-specific physical checklist in this interaction.
7. Use `grind` only when its separate owner gate is enabled, the bean cup/chute are ready, and the
   persisted 60-second rest interval allows it. Never exceed 30 seconds.
8. `scale` may read without a physical-action gate, but `8003` automatically zeros the entry load.
   Start empty for absolute weight or with an empty vessel for net weight. Send `--tare` only as an
   explicitly requested additional re-tare. Always let the wrapper exit scale mode.
9. Never schedule or infer a physical action. Presence, cup placement, and hot-water safety cannot
   be established from BLE telemetry alone.
10. If the workflow is interrupted after load, offer or send `cancel`. Do not probe or replace an
   armed recipe with another recipe.
11. Treat `save-slots` as a persistent configuration change. State that A/B/C will all be replaced
   and obtain explicit user intent before calling it.
12. Do not expose a BLE address, serial number, or telemetry log in a public recipe or issue.
13. Clear a coffee/tea workflow record only after telemetry confirms a terminal machine state. A
   monitoring timeout is an unknown outcome, not completion; preserve the record for recovery.
14. Treat the bridge as the only BLE owner while it runs. Never bypass the direct-command refusal
   or start another app/client against the same Studio connection.
15. Live FreeSolo temperature/pattern changes must retain their separate owner gate and exact
   per-call confirmation. Report hardware verification per control and firmware: pattern is verified
   only on `V12.0D.500`; temperature command/write correctness is verified but physical outlet
   response remains unmeasured.
16. Treat a missing **required** control ACK as an unknown physical outcome. The bridge must fail
   closed after uncertain grinder start/pause/resume and preserve `stop_unconfirmed` on cleanup
   failure. Live setters are different: `8016`/`4510` writes may have only optional `8107`/`8108`
   reports, so report whether one was observed without mislabeling its absence as a failed write.
   Explicit brewer STOP requires echo `4507`; natural completion is report `40511` plus an
   in-tolerance peak meter value, even if the firmware subsequently resets its current meter to zero.
17. Treat `set-settings` and `set-advanced` as persistent writes. Require their separate owner gate
   and exact `--confirm-write`, keep the machine idle with no loaded workflow, compare exact
   readback, and disclose if best-effort rollback cannot be confirmed.
18. Keep recipe target water, cumulative machine output (`40523`), and cup-scale net increase
   separate. None is a quantitative water-supply-level report.

## Physical readiness checklist

Before a physical or remote start, require all of the following:

- Selected tank or direct-feed source available with suitable water.
- Correct beans measured and available to the grinder.
- Omni Dripper and filter installed correctly.
- Receiving vessel larger than 300 ml and below the machine's height limit (about 100 mm).
- Vessel centered on the scale, not touching the machine wall.
- Hands and other objects clear of the spout, dripper, grinder, and cup.
- User aware that the machine can dispense near-boiling water.

For standalone water, beans/filter/dripper are not intrinsically required; instead require a
sufficiently large heat-safe vessel under the correct outlet, the selected tank/direct-feed source, and a
clear water path. For tea,
require the Omni Tea Brewer, leaves, and receiving vessel. For grinding, require beans, a receiving
cup, a clear chute, and hands clear of the motor path. Read `standalone-tools.md` and
`tea-brewing.md` for the exact command-specific checks.

The official interface also provides an on-machine cancel/restart gesture via the right knob.
Keep the user near the machine for any first run or remotely started brew.

## Firmware gate

Every recipe load and preset write performs a read-only preflight. Firmware `V12.0D.500` is in
the tested allowlist. An unrecognized or unreadable firmware blocks writes unless the deployment
owner sets this exact environment value:

```text
XBLOOM_ALLOW_UNTESTED_FIRMWARE=I_ACCEPT_UNTESTED_FIRMWARE
```

This is an owner-level compatibility override, not a normal Agent decision. Do not set it merely
to make a failed task pass. Capture new firmware behavior, update protocol tests, and add the
version to the allowlist only after a controlled no-start validation.

## Hot-water gate

Coffee remote start, standalone water, and tea execution are available capabilities but remain
disabled until the deployment owner sets:

```text
XBLOOM_ENABLE_REMOTE_START=I_UNDERSTAND_REMOTE_HOT_WATER
```

The command additionally requires an exact readiness argument and a recipe loaded less than five
minutes earlier on the same machine with an unchanged file hash:

```text
python scripts/xbloom.py start recipe.yaml --confirm-ready cup-filter-water-beans
```

These gates prevent accidental or stale starts. They do not prove physical safety, so current-turn
user confirmation remains mandatory.

The exact readiness arguments differ by action:

```text
start:     --confirm-ready cup-filter-water-beans
water:     --confirm-ready vessel-water-clear
tea-start / tea-brew: --confirm-ready tea-brewer-water-cup-clear
```

The persistent bridge applies the same owner and readiness gates to `coffee-start`, `tea-start`,
`water-start`, and `grinder-start`. Starting or connecting the daemon does not satisfy physical
readiness.

## Persistent settings gate

Machine-setting and mechanical-tuning writes are disabled until the deployment owner sets:

```text
XBLOOM_ENABLE_SETTINGS_WRITE=I_ACCEPT_PERSISTENT_MACHINE_SETTINGS
```

Each call also needs one action-specific confirmation:

```text
set-settings: --confirm-write persistent-machine-settings
set-advanced: --confirm-write mechanical-tuning
```

The wrapper requires an idle supported firmware, records a baseline, writes only the requested
fields/levels, reads them back, and attempts to restore the baseline on an error or mismatch. The
frames are pinned to APK command encoding, but this project has not physically changed these
settings on a Studio. A failed or unconfirmed rollback requires inspection in the official app or
on-machine settings before another write. Read-only `settings` and `advanced` need no write gate.
The matching bridge commands enforce the same rules; restart only an idle daemon after changing
the owner environment variable because it captures gates at launch.

## Experimental live-adjust gate

The Android app exposes FreeSolo brewer commands for changing temperature target and pour pattern
while a bounded water session is running or paused. Their byte layouts are decoded and tested. A
supervised 2026-07-13 run on firmware `V12.0D.500` physically verified a running
`center → spiral` change. Live-temperature command encoding and a completed write are verified,
but the physical outlet response and paused-state behavior remain unmeasured. Both controls retain
an additional deployment-owner opt-in because they alter a live water path:

```text
XBLOOM_ENABLE_LIVE_ADJUST=I_ACCEPT_UNVERIFIED_LIVE_ADJUST
```

Each command also requires the same exact value through `--confirm-live-adjust`. Do not set either
value on behalf of the user. Restart an idle bridge after changing the environment because the
daemon captures its environment at launch.

This gate applies only to FreeSolo water. A live temperature command changes the remaining target,
not water already delivered; heater and plumbing lag mean the outlet cannot change instantly. A
pattern command changes the remaining outlet motion. Neither command changes target volume or
flow, rewrites a coffee recipe, or enables arbitrary in-recipe edits. Keep a thermometer and a
heat-safe vessel in place for the first supervised temperature A/B, and use physical stop if
behavior differs from the requested target.

On the verified pattern run, command `8016` changed the visible outlet motion without echoing
`8016`; the APK's corresponding machine report `8107` is therefore optional evidence, not a
required ACK. The fail-safe stop was independently confirmed by echo `4507`, followed by quit
`8013` and an idle machine state. Report `40511` remains the distinct natural-completion signal.
That run stopped at about 100 ml of a planned 200 ml RT dispense, so it verifies live pattern and
explicit STOP behavior—not natural target-volume completion.

## Grinder gate and rest lock

The standalone grinder uses its own owner opt-in because it presents a motor hazard without hot
water:

```text
XBLOOM_ENABLE_REMOTE_GRINDER=I_UNDERSTAND_REMOTE_GRINDER
python scripts/xbloom.py grind --size 62 --rpm 100 --seconds 10 \
  --confirm-ready beans-cup-clear
```

Each run is limited to 30 seconds. The wrapper records a conservative runtime-plus-60-second block
under the state directory before sending START. Do not delete or relocate that record to bypass a
cooldown. STOP and QUIT are attempted from a `finally` block on normal errors or interruption;
physical controls remain the final fallback if the process or BLE adapter fails completely.

## Recovery

Use the least invasive recovery path:

1. Stop monitoring with Ctrl+C if only the terminal is stuck.
2. If `start`/`tea-start` reports `completion_unconfirmed` (exit 3), run `monitor` to reattach or
   `cancel` to stop. Both commands reuse the machine address stored by load and do not scan first.
3. Run `python scripts/xbloom.py cancel` for an armed, waiting, or active workflow.
4. Use the machine's physical cancel control if BLE is unavailable.
5. Move the cup only after the machine has stopped dispensing.
6. If the local armed-state file is stale, run `cancel` once to clear it safely.

For a bridge-owned activity, inspect `bridge status` and `bridge events` first, then use
`bridge cancel`. An idle `bridge stop` is clean; `bridge stop --force` may send a physical stop and
must be treated as an action, not process cleanup. If the bridge process crashes during grinding,
its persisted `in_progress` rest record intentionally blocks another run until the operator has
confirmed the machine stopped and the conservative cooldown has elapsed.

Coffee, tea, grinder-rest, bridge endpoint/token, bridge log, and the external Python runtime live under
`~/.xbloom-studio-brew/` by default. Override the directory with `XBLOOM_SKILL_STATE_DIR` for tests
or managed deployments; `XBLOOM_SKILL_RUNTIME_DIR` can override only the virtual environment. The
bridge binds only to loopback and authenticates each JSON-line request
with its random local token; this is local process isolation, not a remotely exposed security
boundary.

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

## Physical readiness checklist

Before a physical or remote start, require all of the following:

- Water tank filled with suitable water.
- Correct beans measured and available to the grinder.
- Omni Dripper and filter installed correctly.
- Receiving vessel larger than 300 ml and below the machine's height limit (about 100 mm).
- Vessel centered on the scale, not touching the machine wall.
- Hands and other objects clear of the spout, dripper, grinder, and cup.
- User aware that the machine can dispense near-boiling water.

For standalone water, beans/filter/dripper are not intrinsically required; instead require a
sufficiently large heat-safe vessel under the correct outlet, the selected tank/tap source, and a
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

Coffee remote start, standalone water, and tea execution are shipped capabilities but remain
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
tea-start: --confirm-ready tea-brewer-water-cup-clear
```

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

Coffee, tea, and grinder-rest records live under `~/.xbloom-studio-brew/` by default. Override the
directory with `XBLOOM_SKILL_STATE_DIR` for tests or managed deployments.

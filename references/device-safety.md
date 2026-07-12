# BLE and hot-water safety

The BLE protocol is unofficial and reverse-engineered. It is not an xBloom API. The vendored
implementation has been tested by its upstream project only on firmware `V12.0D.500`; this
skill also completed its initial hardware validation on that firmware.

## Command risk matrix

| Command | Machine effect | Brew-control opcode |
| --- | --- | --- |
| `doctor` | Local dependency check; optional passive scan. | None |
| `scan` | Discovers nearby advertisements. | None |
| `probe` | Opens an app-style session and asks for status/firmware. | None (`0xA4`, `0x56`) |
| `validate` | Parses and validates a local recipe. | None |
| `load` | Writes guarded recipe frames and leaves the machine armed. Does not brew. | None |
| `monitor` | Subscribes to state and scale notifications. | None |
| `save-slots` | Persistently overwrites all three on-machine A/B/C presets. Does not brew. | None |
| `cancel` | Cancels/exits an armed or active operation. | `0x47` cancel |
| `start` | Commits and starts an armed recipe; can grind and dispense near-boiling water. | `0x42`, sometimes `0x46` |

## Mandatory operating rules

1. Do not touch BLE when the user only asked for a recipe. Generate and validate the file.
2. Use `scripts/xbloom.py`; never invoke the vendored client or raw protocol builders directly.
3. Run `probe` only before loading, never while an armed-state record exists.
4. Validate before load. Treat validation errors as blockers; do not weaken limits ad hoc.
5. Load is the default device action. It arms the recipe and lets the user approve physically.
6. Use `start` only when the deployment owner enabled remote start and the user explicitly
   confirms in the current interaction that water, beans, filter, dripper, and cup are ready.
7. Never schedule or infer a remote start. Presence, cup placement, and hot-water safety cannot
   be established from BLE telemetry alone.
8. If the workflow is interrupted after load, offer or send `cancel`. Do not probe or replace an
   armed recipe with another recipe.
9. Treat `save-slots` as a persistent configuration change. State that A/B/C will all be replaced
   and obtain explicit user intent before calling it.
10. Do not expose a BLE address, serial number, or telemetry log in a public recipe or issue.

## Physical readiness checklist

Before a physical or remote start, require all of the following:

- Water tank filled with suitable water.
- Correct beans measured and available to the grinder.
- Omni Dripper and filter installed correctly.
- Receiving vessel larger than 300 ml and below the machine's height limit (about 100 mm).
- Vessel centered on the scale, not touching the machine wall.
- Hands and other objects clear of the spout, dripper, grinder, and cup.
- User aware that the machine can dispense near-boiling water.

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

## Remote-start gate

Remote start is shipped as a core BLE capability but is disabled until the deployment owner sets:

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

## Recovery

Use the least invasive recovery path:

1. Stop monitoring with Ctrl+C if only the terminal is stuck.
2. Run `python scripts/xbloom.py cancel` for an armed, waiting, or active workflow.
3. Use the machine's physical cancel control if BLE is unavailable.
4. Move the cup only after the machine has stopped dispensing.
5. If the local armed-state file is stale, run `cancel` once to clear it safely.

The state file lives under `~/.xbloom-studio-brew/armed-state.json` by default. Override its
directory with `XBLOOM_SKILL_STATE_DIR` for tests or managed deployments.

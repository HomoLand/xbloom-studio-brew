---
name: xbloom-studio-brew
description: Design bean-specific hot pour-over and Americano-style flash-brew recipes for xBloom Studio, research cited roaster/cafe/xPod references, run guarded Omni Tea Brewer recipes, dial in by taste, and operate bundled local BLE for diagnostics, scale, grinder, temperature/volume water, recipe load, presets, monitoring, cancel, and explicitly gated physical starts. Use for xBloom Studio, Omni Dripper, xPod/NFC Recipe Cards, Omni Tea Brewer, coffee or tea recipes, iced coffee, C40 conversion, WAIT troubleshooting, electronic-scale readings, standalone grinding/water, or direct xBloom Bluetooth control.
---

# xBloom Studio Brew

Turn bean information into a concrete, validated xBloom Studio recipe and, when explicitly
requested, load or run it through the bundled local BLE controller.

Resolve `<skill-dir>` to the absolute directory containing this file. Run only the scripts inside
that directory; never recreate protocol frames in the conversation.

## Choose the workflow

Classify the request before acting:

- **Recipe only:** design, save, validate, and explain the recipe. Do not scan or connect.
- **Research and compare:** find credible public bean/recipe sources, distinguish native xBloom
  recipes from adapted manual brews, and let the user choose before creating an executable recipe.
- **xPod reference:** preserve roaster intent but classify it `xPod-native`; adapt explicitly before
  using loose beans with Omni, and never assume an NFC card contains the full recipe payload.
- **Dial-in:** inspect the previous recipe and tasted result, then change one variable.
- **Load to machine:** validate, preflight firmware/state, and arm the recipe. Do not start it.
- **Brew now:** load first, then require current physical-readiness confirmation before `start`.
- **Tea:** use the dedicated tea schema and Omni Tea Brewer protocol; keep load and execute separate.
- **Scale:** account for the firmware's mandatory entry auto-zero; choose an empty-platform
  baseline for absolute object weight or a pre-positioned empty vessel for net contents.
- **Standalone grinder/water:** use their specific owner gate, readiness phrase, and cleanup flow.
- **Preset slots:** require explicit intent to overwrite all A/B/C presets.
- **Diagnostics:** use read-only `doctor`, `scan`, `probe`, or `monitor`; use `cancel` for recovery.

Do not produce espresso recipes. xBloom Studio brews pour-over, not espresso. When the user asks
for iced Americano, offer an **Americano-style flash brew** and state the distinction briefly.

## Design a recipe

Read `references/recipe-design.md` when creating or adjusting a recipe. Read
`references/recipe-schema.md` before writing YAML.

1. Extract drink style, roast, roast date, process, origin/variety, tasting notes, water, and the
   user's flavor target. Prefer the user's target over the roaster's notes.
2. When an exact roaster, coffee, lot, or xPod is identifiable, or the user asks for expert/public
   recipes, read `references/web-enrichment.md` and search first-party public sources if web tools
   are available. If credible recipes exist, show the Skill baseline and up to two cited adaptations
   for selection. Never load an external adaptation before the user chooses it.
3. If details are missing after research, use reasonable assumptions and state them. Default to 15 g coffee,
   240 g final water, Omni Dripper 2, and filtered water; never assume unknown water is RO.
4. Choose the smallest useful pour count and a conservative first-cup profile.
5. For flash brew, separate hot machine water from ice and label the hot and final ratios.
   For an intentional bypass recipe, keep extraction pours and final bypass explicit; bypass is
   machine water, not display-only metadata. Use `RT`/`BP` only when that mode is intentional.
6. Copy `assets/hot-template.yaml` or `assets/flash-brew-template.yaml` to a user/workspace path.
   Never modify the installed template in place.
7. Fill every field with a concrete value. Preserve `stage_temps: [110.0, 90.0]`.
8. Validate the finished local file:

```text
python <skill-dir>/scripts/xbloom.py validate <recipe.yaml>
```

9. Fix every validation error rather than bypassing the guard. Present the validated recipe, its
   assumptions, and exactly one first correction for the next cup.

Do not claim a generated recipe has been taste-validated. After the user tastes it, use the result
and drawdown behavior to adjust one variable at a time.

## Prepare local BLE

Read `references/device-safety.md` completely before the first BLE action in a task.
For a scale, grinder, or water request, also read `references/standalone-tools.md`. For tea, read
`references/tea-brewing.md`.

Bootstrap once per installed copy:

```text
python <skill-dir>/scripts/bootstrap.py
```

Check runtime and discover the machine:

```text
python <skill-dir>/scripts/xbloom.py doctor --scan
python <skill-dir>/scripts/xbloom.py scan
```

If exactly one xBloom is nearby, commands can scan automatically. If several exist, put the
selected identifier before the subcommand:

```text
python <skill-dir>/scripts/xbloom.py --address <ble-address-or-uuid> probe
```

Treat addresses and serials as private local identifiers. Do not reproduce them in public output.

## Load without starting

Use this as the normal device workflow:

```text
python <skill-dir>/scripts/xbloom.py probe
python <skill-dir>/scripts/xbloom.py validate <recipe.yaml>
python <skill-dir>/scripts/xbloom.py load <recipe.yaml>
```

`load` performs its own read-only firmware/state preflight, transmits only guarded load frames,
waits for the machine's `armed` state, and records the recipe hash. Confirm that JSON reports
`"status": "armed"` and `"remote_start_sent": false`.

Tell the user the machine is armed and can be approved physically. Do not run `probe` again while
armed. To exit instead, run:

```text
python <skill-dir>/scripts/xbloom.py cancel
```

## Start and monitor a coffee brew

Remote start is a shipped core capability, not an add-on, but it has owner and per-brew gates.
Never set the owner opt-in yourself. Never infer physical readiness from BLE.

Only after the user explicitly confirms water, beans, filter, dripper, cup, and clear surroundings
in the current interaction, and only when the deployment owner has enabled remote start, run:

```text
python <skill-dir>/scripts/xbloom.py start <same-recipe.yaml> --confirm-ready cup-filter-water-beans
```

The recipe must be unchanged, armed on the same machine, and loaded less than five minutes ago.
The command streams telemetry until completion or timeout. For listen-only telemetry, use:

```text
python <skill-dir>/scripts/xbloom.py monitor --duration 300
```

If anything is uncertain, cancel. Never schedule an unattended start.

## Use the Omni Tea Brewer

Tea requires the dedicated siphon accessory and schema; never emulate it with a coffee no-grind
recipe. Copy the closest official template from `assets/tea-*-official.yaml` to a workspace path,
then read `references/tea-brewing.md`, adapt one variable at a time, and validate:

```text
python <skill-dir>/scripts/xbloom.py tea-validate <tea.yaml>
python <skill-dir>/scripts/xbloom.py tea-load <tea.yaml>
```

`tea-load` only uploads the recipe. It must report `"status": "tea_loaded"` and
`"remote_start_sent": false`. Execute only after the owner hot-water gate is enabled and the user
currently confirms the Omni Tea Brewer, leaves, tank, receiving vessel, and clear surroundings:

```text
python <skill-dir>/scripts/xbloom.py tea-start <same-tea.yaml> \
  --confirm-ready tea-brewer-water-cup-clear
```

Never schedule it. Use `cancel` to exit a loaded tea recipe or recover from an unsafe operation.

## Use the standalone tools

Read `references/standalone-tools.md` and keep recipe states separate from FreeSolo modes.

Electronic-scale reading is low risk and does not require a physical-action environment gate:

```text
python <skill-dir>/scripts/xbloom.py scale --duration 30
```

The official `8003` scale-enter command automatically zeros the load already present. For an
object's absolute weight, require the platform to be empty before starting, wait until JSON reports
`"status": "ready"`, then ask the user to place the object. For net contents, put the empty vessel
on the platform before starting and add contents only after `ready`. An object present at entry
will read zero; removing it will read negative. There is no decoded enter-without-zero command.

Add `--tare` only when the user explicitly requests an *additional* re-tare after entry. Do not
describe the default as "without tare" or imply that omitting `--tare` preserves the pre-entry
absolute load.

The grinder is a motor action. Never set its owner opt-in yourself. After current confirmation of
beans, receiving cup, clear chute, and clear hands, an enabled deployment may run at most 30 s:

```text
python <skill-dir>/scripts/xbloom.py grind --size 62 --rpm 100 --seconds 10 \
  --confirm-ready beans-cup-clear
```

Respect the persisted 60-second rest lock; do not delete it to retry. Standalone water is a
hot-water action and uses the hot-water owner gate. Require a suitable centered vessel, filled tank,
correct water path, and clear surroundings in the current interaction:

```text
python <skill-dir>/scripts/xbloom.py water --volume 250 --temp 85 --flow 3.5 \
  --pattern center --water-source auto --confirm-ready vessel-water-clear
```

Use `--temp RT` for the official room-temperature/pass-through setting. RT does not actively cool
the source water to an exact 20 C, and it remains a guarded physical water-dispense action.
`--water-source auto` follows the source reported by the machine; if that report is unavailable,
require the user to select `tank` or `tap` explicitly instead of guessing.

Never claim a grind or dispense completed unless its command reports success. On uncertainty, use
the machine's physical stop/cancel after the wrapper's automatic STOP/QUIT cleanup.

## Save A/B/C presets

Explain that this replaces all three on-machine presets, then require explicit user approval.
Validate each file and write all three in A/B/C order:

```text
python <skill-dir>/scripts/xbloom.py save-slots <A.yaml> <B.yaml> <C.yaml>
```

Confirm JSON reports `"status": "saved"` and `"brew_started": false`. Do not use preset writes
as a substitute for loading one temporary recipe.

## Handle failures

- Runtime missing: run `scripts/bootstrap.py`; do not install packages globally.
- No machine found: confirm Bluetooth is on, the Agent is executing locally, the phone app is not
  holding the connection, and the machine is nearby.
- Multiple machines: select one with `--address` or `XBLOOM_ADDRESS`.
- Unknown firmware: stop. Only the deployment owner may use the explicit override documented in
  `references/device-safety.md` after controlled validation.
- Armed-state record exists: monitor or cancel; do not probe or load over it.
- Tea-loaded-state record exists: execute the unchanged recipe while currently ready, or cancel.
- Grinder cooldown active: wait for the reported remainder; never bypass the rest record.
- `WAIT` or slow drawdown: follow the physical checks in `references/recipe-design.md`, then change
  one recipe variable.
- Interrupted or unsafe operation: run `cancel` and use the machine's physical control if BLE fails.

## Output contract

Match the user's language. Keep the response compact but include:

- Bean assumptions and flavor target.
- For coffee: dose, extraction water, bypass/final water where applicable, grind, RPM, and expected time.
- For tea: leaf mass, each steep's programmed water/temperature/pause, expected siphon output, and
  whether the recipe is official or adapted.
- Every pour: ml, temperature, pattern, agitation, pause, and flow.
- Validation result and file path.
- Cited public sources, source brewer, original device, match quality, and every adaptation when
  web enrichment was used. Keep citations outside executable YAML.
- Device state only if a BLE command was requested and actually run.
- For scale/grinder/water, report the requested values and confirmed completion/exit without
  publishing the machine identifier.
- One taste-based next adjustment.

Never say a brew started, completed, or was cancelled unless the corresponding command and machine
state support it. Distinguish `loaded/armed` from `started/brewing`.

## References

- Read `references/recipe-design.md` for bean logic, flash brew, C40 conversion, and dial-in.
- Read `references/web-enrichment.md` when researching bean metadata or public expert recipes.
- Read `references/recipe-schema.md` for the exact guarded file contract.
- Read `references/device-safety.md` before BLE writes or remote start.
- Read `references/standalone-tools.md` for FreeSolo scale, grinder, and brewer commands.
- Read `references/tea-brewing.md` for tea requirements, official templates, schema, and workflow.
- Read `references/deployment.md` for Codex/Hermes installation, publication, and environment setup.
- Read `references/apk-capability-matrix.md` before claiming app parity or adding decoded commands.
- Read `references/sources.md` when checking provenance or making hardware/protocol claims.

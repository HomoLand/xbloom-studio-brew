---
name: xbloom-studio-brew
description: Design bean-specific hot pour-over and Americano-style flash-brew recipes for xBloom Studio, validate guarded YAML, dial in by taste, and operate the machine through bundled local BLE for scan, firmware probe, recipe load, monitoring, cancel, preset slots, and explicitly gated remote start. Use when the user mentions xBloom Studio, Omni Dripper, coffee-bean recipes, iced coffee, C40 conversion, WAIT troubleshooting, or direct xBloom Bluetooth control.
---

# xBloom Studio Brew

Turn bean information into a concrete, validated xBloom Studio recipe and, when explicitly
requested, load or run it through the bundled local BLE controller.

Resolve `<skill-dir>` to the absolute directory containing this file. Run only the scripts inside
that directory; never recreate protocol frames in the conversation.

## Choose the workflow

Classify the request before acting:

- **Recipe only:** design, save, validate, and explain the recipe. Do not scan or connect.
- **Dial-in:** inspect the previous recipe and tasted result, then change one variable.
- **Load to machine:** validate, preflight firmware/state, and arm the recipe. Do not start it.
- **Brew now:** load first, then require current physical-readiness confirmation before `start`.
- **Preset slots:** require explicit intent to overwrite all A/B/C presets.
- **Diagnostics:** use read-only `doctor`, `scan`, `probe`, or `monitor`; use `cancel` for recovery.

Do not produce espresso recipes. xBloom Studio brews pour-over, not espresso. When the user asks
for iced Americano, offer an **Americano-style flash brew** and state the distinction briefly.

## Design a recipe

Read `references/recipe-design.md` when creating or adjusting a recipe. Read
`references/recipe-schema.md` before writing YAML.

1. Extract drink style, roast, roast date, process, origin/variety, tasting notes, water, and the
   user's flavor target. Prefer the user's target over the roaster's notes.
2. If details are missing, use reasonable assumptions and state them. Default to 15 g coffee,
   240 g final water, Omni Dripper 2, and filtered water; never assume unknown water is RO.
3. Choose the smallest useful pour count and a conservative first-cup profile.
4. For flash brew, separate hot machine water from ice and label the hot and final ratios.
5. Copy `assets/hot-template.yaml` or `assets/flash-brew-template.yaml` to a user/workspace path.
   Never modify the installed template in place.
6. Fill every field with a concrete value. Preserve `stage_temps: [110.0, 90.0]`.
7. Validate the finished local file:

```text
python <skill-dir>/scripts/xbloom.py validate <recipe.yaml>
```

8. Fix every validation error rather than bypassing the guard. Present the validated recipe, its
   assumptions, and exactly one first correction for the next cup.

Do not claim a generated recipe has been taste-validated. After the user tastes it, use the result
and drawdown behavior to adjust one variable at a time.

## Prepare local BLE

Read `references/device-safety.md` completely before the first BLE action in a task.

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

## Start and monitor a brew

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
- `WAIT` or slow drawdown: follow the physical checks in `references/recipe-design.md`, then change
  one recipe variable.
- Interrupted or unsafe operation: run `cancel` and use the machine's physical control if BLE fails.

## Output contract

Match the user's language. Keep the response compact but include:

- Bean assumptions and flavor target.
- Dose, hot water, ice/final water where applicable, grind, RPM, and expected time.
- Every pour: ml, temperature, pattern, agitation, pause, and flow.
- Validation result and file path.
- Device state only if a BLE command was requested and actually run.
- One taste-based next adjustment.

Never say a brew started, completed, or was cancelled unless the corresponding command and machine
state support it. Distinguish `loaded/armed` from `started/brewing`.

## References

- Read `references/recipe-design.md` for bean logic, flash brew, C40 conversion, and dial-in.
- Read `references/recipe-schema.md` for the exact guarded file contract.
- Read `references/device-safety.md` before BLE writes or remote start.
- Read `references/deployment.md` for Codex/Hermes installation, publication, and environment setup.
- Read `references/sources.md` when checking provenance or making hardware/protocol claims.

## Acknowledgements

This Skill stands on two excellent MIT-licensed community projects:

- [ryunana/xbloom-studio-recipe-skill](https://github.com/ryunana/xbloom-studio-recipe-skill)
  supplied the recipe-engineering foundation, including bean archetypes, dial-in heuristics, and
  C40 conversion starting points.
- [Janczykkkko/xbloom-ble](https://github.com/Janczykkkko/xbloom-ble) supplied the reverse-engineered
  xBloom Studio BLE protocol, client, telemetry parser, and protocol tests.

Thank you to both maintainers for making their work available to the coffee and Agent communities.
See `THIRD_PARTY_NOTICES.md` and `licenses/` for pinned upstream commits and complete license texts.

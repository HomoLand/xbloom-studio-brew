---
name: xbloom-studio-brew
description: Design bean-specific hot pour-over and Americano-style flash-brew recipes for xBloom Studio, research cited roaster/cafe/xPod references, import/query/sync a private app-visible coffee/tea catalog, preview or explicitly add local account recipes, run guarded Omni Tea Brewer recipes, dial in by taste, and operate bundled local BLE for diagnostics, settings, scale, grinder, temperature/volume water, recipe load, A/B/C presets, monitoring, cancel, persistent pause/resume, and explicitly gated physical starts. Use for xBloom Studio, Omni Dripper, xPod/NFC Recipe Cards, Omni Tea Brewer, official or saved coffee/tea recipes, iced coffee, C40 conversion, WAIT troubleshooting, electronic-scale readings, standalone grinding/water, or direct xBloom Bluetooth control.
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
- **Private catalog:** import authorized App/API or decoded-MMKV JSON; ephemerally sync official,
  created, Product/xPod, and shared account recipes; preview or explicitly add local recipes.
- **xPod reference:** preserve roaster intent but classify it `xPod-native`; adapt explicitly before
  using loose beans with Omni, and never assume an NFC card contains the full recipe payload.
- **Dial-in:** inspect the previous recipe and tasted result, then change one variable.
- **Load to machine:** validate, preflight firmware/state, and arm the recipe. Do not start it.
- **Brew now:** load first, then require current physical-readiness confirmation before `start`.
- **Tea:** use the dedicated tea schema and Omni Tea Brewer protocol; keep load and execute separate.
- **Scale:** account for the firmware's mandatory entry auto-zero; choose an empty-platform
  baseline for absolute object weight or a pre-positioned empty vessel for net contents.
- **Standalone grinder/water:** use their specific owner gate, readiness phrase, and cleanup flow.
- **Persistent bridge:** prefer the long-lived local bridge for device work so one process owns
  BLE and serializes coffee, tea, scale, grinder, water, presets, settings, and tuning writes.
- **Preset slots:** require explicit intent to overwrite all A/B/C presets.
- **Diagnostics/settings:** use read-only `doctor`, `scan`, `probe`, `settings`, `advanced`, or
  `monitor`; use `cancel` for recovery. Persistent writes require their own owner gate.

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
7. Fill every public field with a concrete value. Do not add undocumented protocol fields.
8. Validate the finished local file:

```text
python <skill-dir>/scripts/xbloom.py validate <recipe.yaml>
```

9. Fix every validation error rather than bypassing the guard. Present the validated recipe, its
   assumptions, and exactly one first correction for the next cup.

Do not claim a generated recipe has been taste-validated. After the user tastes it, use the result
and drawdown behavior to adjust one variable at a time.

## Use the private recipe catalog

Read `references/catalog.md` before importing or syncing app-visible recipes. Catalog operations
are offline from BLE and require no machine connection:

```text
python <skill-dir>/scripts/xbloom.py catalog status
python <skill-dir>/scripts/xbloom.py catalog import-json <authorized-export.json>
python <skill-dir>/scripts/xbloom.py catalog import-mmkv <decoded-mmkv.json>
python <skill-dir>/scripts/xbloom.py catalog list --kind coffee --executable
python <skill-dir>/scripts/xbloom.py catalog list --kind tea
python <skill-dir>/scripts/xbloom.py catalog show <id-or-name>
python <skill-dir>/scripts/xbloom.py catalog export <id> <workspace-recipe.yaml>
python <skill-dir>/scripts/xbloom.py catalog login-sync --region <china|international>
python <skill-dir>/scripts/xbloom.py catalog push <recipe.yaml> --region <china|international>
```

Treat “all” as all records in the supplied export or visible to the user's own account and region,
not a global xBloom database. Keep the normalized catalog private: redistribution rights default
to unknown. xPod and J20 entries are reference-only; tea exports use the dedicated tea path.

`login-sync` reads all five account categories by default and was live-service verified against the
China tenant on 2026-07-14. Supply email/password only through `XBLOOM_ACCOUNT_EMAIL` and
`XBLOOM_ACCOUNT_PASSWORD` (or the hidden interactive password prompt); never print or persist them.
The older explicit-form `sync` remains available through `--config`/`XBLOOM_CLOUD_CONFIG`.

`catalog push` is an offline preview by default. Remote use is add-only and idempotent: it refuses
same-name/different-parameter conflicts. Run `--apply --confirm-write own-account-cloud-recipe`
only after the user explicitly approves that exact recipe and account mutation in the current
interaction. Never use a live account merely to test the write path; tests must mock the endpoint.

## Prepare local BLE

Read `references/device-safety.md` completely before the first BLE action in a task.
For a scale, grinder, or water request, also read `references/standalone-tools.md`. For tea, read
`references/tea-brewing.md`.

Bootstrap once per user/Agent environment. The virtual environment is stored in the writable
state directory, not inside the installed Skill, so upgrades and read-only package caches are safe:

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

For pause/resume or control-grade telemetry, start the bundled loopback-only bridge. Starting the
daemon does not scan, connect, grind, or dispense water:

```text
python <skill-dir>/scripts/xbloom.py bridge start
python <skill-dir>/scripts/xbloom.py bridge status
python <skill-dir>/scripts/xbloom.py bridge connect
python <skill-dir>/scripts/xbloom.py bridge events --since 0
```

The bridge is the sole BLE owner while it runs. Direct one-shot BLE commands deliberately refuse
to race it; use the matching `bridge` workflow or stop the idle daemon first. `bridge stop` refuses
during an activity; `bridge stop --force` first sends that activity's guarded stop/cancel path.
Bridge operations cover coffee, tea, scale, grinder, water, presets, persistent settings, advanced
tuning, and continuous events. Read `references/deployment.md` for daemon state, external runtime,
and deployment details.

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

Remote start is an available core capability, not an add-on, but it has owner and per-brew gates.
Never set the owner opt-in yourself. Never infer physical readiness from BLE.

Only after the user explicitly confirms water, beans, filter, dripper, cup, and clear surroundings
in the current interaction, and only when the deployment owner has enabled remote start, run:

```text
python <skill-dir>/scripts/xbloom.py start <same-recipe.yaml> --confirm-ready cup-filter-water-beans
```

The recipe must be unchanged, armed on the same machine, and loaded less than five minutes ago.
The command aggregates weight progress to at most one update per second and emits a final summary.
Claim successful completion only when that summary reports `"completion_confirmed": true`.
`"terminal_confirmed": true` means the workflow ended and allows the wrapper to clear its record;
an `idle` terminal without `ready`/`complete` does not prove a successful cup. For listen-only
telemetry, use:

```text
python <skill-dir>/scripts/xbloom.py monitor --duration 300
```

If monitoring reaches its duration without a terminal machine state, `start` exits with code 3,
reports `completion_unconfirmed`, and preserves the machine binding for `monitor` or `cancel`.
Never interpret that timeout as a failed brew or a completed brew.

Interpret liquid telemetry as three separate measurements:

- `target_dispensed_water_ml`: programmed recipe water, including machine bypass when present.
- `dispensed_water_ml`: the machine's cumulative output for this operation (report `40523`), not
  the amount remaining in a reservoir or direct-feed supply.
- `cup_weight_g` / `cup_delta_g`: raw cup-scale weight and its net increase from the observed
  operation baseline. Retained water, grounds, ice, evaporation, and timing can make this differ
  from the machine meter.

Do not claim the protocol exposes supply inventory; it only reports whether water is available and
which source is selected.

If anything is uncertain, cancel. Never schedule an unattended start.

For a coffee brew that must support pause/resume, keep the whole load/start flow on the bridge:

```text
python <skill-dir>/scripts/xbloom.py bridge coffee-load <recipe.yaml>
python <skill-dir>/scripts/xbloom.py bridge coffee-start \
  --confirm-ready cup-filter-water-beans
python <skill-dir>/scripts/xbloom.py bridge pause
python <skill-dir>/scripts/xbloom.py bridge resume
python <skill-dir>/scripts/xbloom.py bridge cancel
```

Only send pause/resume when bridge status shows a compatible running/paused activity. Command
`40518` is state-sensitive: the same app command confirms start only after a fresh
`awaiting_confirm` state and pauses while running. It is not an unconditional start opcode or a
recipe rewrite.

## Use the Omni Tea Brewer

Tea requires the dedicated siphon accessory and schema; never emulate it with a coffee no-grind
recipe. Copy the closest official template from `assets/tea-*-official.yaml` to a workspace path,
then read `references/tea-brewing.md`, adapt one variable at a time, and validate:

Treat each stage's 80/90 ml as programmed chamber-fill water. The app's ~120/240/360 ml selector
is approximate finished siphon output; firmware owns the post-soak finish and reports it as a
`bypass` phase. It is not the generic configurable coffee bypass, and 120 ml must never be encoded
as one stage merely to match the display.

```text
python <skill-dir>/scripts/xbloom.py tea-validate <tea.yaml>
python <skill-dir>/scripts/xbloom.py tea-load <tea.yaml>
```

`tea-load` only uploads the recipe. It must report `"status": "tea_loaded"` and
`"remote_start_sent": false`. Execute only after the owner hot-water gate is enabled and the user
currently confirms the Omni Tea Brewer, leaves, selected water supply, receiving vessel, and clear
surroundings:

```text
python <skill-dir>/scripts/xbloom.py tea-start <same-tea.yaml> \
  --confirm-ready tea-brewer-water-cup-clear
```

When load and execution should remain on one BLE connection, the same gates and checklist apply to:

```text
python <skill-dir>/scripts/xbloom.py tea-brew <tea.yaml> \
  --confirm-ready tea-brewer-water-cup-clear
```

`tea-brew` still emits distinct `tea_loaded` and `start_accepted` states and preserves a recovery
record until terminal telemetry. Soaking, paused, restarted, and soak-time reports are surfaced
when firmware emits them.

When the bridge is running, keep tea on that owner instead:

```text
python <skill-dir>/scripts/xbloom.py bridge tea-load <tea.yaml>
python <skill-dir>/scripts/xbloom.py bridge tea-start \
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

For an interactive absolute-weight request, keep the platform empty through entry, surface
`"status": "ready"` immediately, then tell the user to place the object. Use a 60-90 second session
when chat latency requires it and report only a stable, plausible positive reading. An all-zero
session did not measure the object's absolute weight; explain the baseline and retry from empty.

With a running bridge, the non-blocking equivalent keeps BLE ownership stable and permits an
explicit re-tare or early exit:

```text
python <skill-dir>/scripts/xbloom.py bridge scale-start --duration 90
python <skill-dir>/scripts/xbloom.py bridge scale-tare
python <skill-dir>/scripts/xbloom.py bridge cancel
```

The grinder is a motor action. Never set its owner opt-in yourself. After current confirmation of
beans, receiving cup, clear chute, and clear hands, an enabled deployment may run at most 30 s:

```text
python <skill-dir>/scripts/xbloom.py grind --size 62 --rpm 100 --seconds 10 \
  --confirm-ready beans-cup-clear
```

Respect the persisted 60-second rest lock; do not delete it to retry. Standalone water is a
hot-water action and uses the hot-water owner gate. Require a suitable centered vessel, available
water at the selected source, the correct water path, and clear surroundings in the current
interaction:

```text
python <skill-dir>/scripts/xbloom.py water --volume 250 --temp 85 --flow 3.5 \
  --pattern center --water-source auto --confirm-ready vessel-water-clear
```

Use `--temp RT` for the official room-temperature/pass-through setting. RT does not actively cool
the source water to an exact 20 C, and it remains a guarded physical water-dispense action.
`--water-source auto` follows the source reported by the machine; if that report is unavailable,
require the user to select `tank` or `tap` explicitly instead of guessing. Here `tap` is the
protocol/CLI compatibility name for Studio's direct-feed/auto-refill source, not a promise about
the installation's plumbing.

Use the bridge when interactive grinder or FreeSolo-water pause/resume is required:

```text
python <skill-dir>/scripts/xbloom.py bridge grinder-start --size 62 --rpm 100 --seconds 10 \
  --confirm-ready beans-cup-clear
python <skill-dir>/scripts/xbloom.py bridge water-start --volume 250 --temp 85 --flow 3.5 \
  --pattern center --water-source auto --confirm-ready vessel-water-clear
python <skill-dir>/scripts/xbloom.py bridge pause
python <skill-dir>/scripts/xbloom.py bridge resume
```

For bridge grinder control, a missing start/pause/resume ACK triggers fail-closed STOP/QUIT and
must not be retried past the persisted rest lock. For bridge water, claim completion only when
`last_operation.result` is `complete`. A result of `completion_unconfirmed` or
`safety_timeout_stopped`, or a phase of `control_unconfirmed`/`stop_unconfirmed`, requires
recovery and physical verification.

The decoded FreeSolo live-temperature and live-pattern commands are intentionally separate from
pause/resume. They change only the target for the remaining bounded water session: temperature has
physical heating lag, pattern changes the outlet motion, and neither changes total volume or flow.
They do not modify coffee recipe pours. A running `center → spiral` pattern change is physically
verified on firmware `V12.0D.500`; command `8016` may apply without an echo, while report `8107` is
optional and reported separately when observed. Live-temperature command encoding and its
completed BLE write are verified, but physical outlet response is unmeasured.
Keep both controls behind their owner gate and exact per-call confirmation, and report verification
per control and firmware exactly as documented in `references/device-safety.md`.

Never claim a grind or dispense completed unless its command reports success. On uncertainty, use
the machine's physical stop/cancel after the wrapper's automatic STOP/QUIT cleanup.

## Inspect or change Studio settings

Read persistent user settings and APK-defined mechanical tuning without a write gate:

```text
python <skill-dir>/scripts/xbloom.py settings
python <skill-dir>/scripts/xbloom.py advanced
```

If the bridge is running, use `bridge settings` and `bridge advanced` instead.

Persistent changes are implemented with APK-exact commands, idle/firmware preflight,
read-after-write comparison, and best-effort rollback. They have deterministic protocol coverage
but have not been physically written by this project, so never set their owner opt-in on the
user's behalf:

```text
XBLOOM_ENABLE_SETTINGS_WRITE=I_ACCEPT_PERSISTENT_MACHINE_SETTINGS
python <skill-dir>/scripts/xbloom.py set-settings --weight-unit g \
  --temperature-unit C --water-source tank --display medium \
  --confirm-write persistent-machine-settings
python <skill-dir>/scripts/xbloom.py set-advanced --pour-radius-level 3 \
  --vibration-level 3 --confirm-write mechanical-tuning
```

For a running bridge, restart it while idle after setting the owner environment variable, then use
the matching `bridge set-settings` or `bridge set-advanced` command. The daemon captures owner
gates at startup.

`set-advanced` derives five radius levels from the individual machine's reported baseline and uses
the APK's six amplitude levels. Do not substitute raw values, write during a loaded/running
workflow, or report success unless exact readback matches. These persistent controls are unrelated
to per-recipe vibration timing and live FreeSolo pattern changes.

## Save A/B/C presets

Explain that this replaces all three on-machine presets, then require explicit user approval.
Validate each file for both recipe safety and lossless slot representation, then write all three
in A/B/C order:

```text
python <skill-dir>/scripts/xbloom.py validate <A.yaml> --slot
python <skill-dir>/scripts/xbloom.py validate <B.yaml> --slot
python <skill-dir>/scripts/xbloom.py validate <C.yaml> --slot
python <skill-dir>/scripts/xbloom.py save-slots <A.yaml> <B.yaml> <C.yaml>
```

Add `--scale on off on` only when the user explicitly wants different on-brew scale behavior for
A, B, and C; the default is `on on on`.

With a running bridge, use `bridge save-slots <A.yaml> <B.yaml> <C.yaml>` so the daemon remains the
sole connection owner.

Confirm JSON reports `"status": "saved"` and `"brew_started": false`. Do not use preset writes
as a substitute for loading one temporary recipe. A/B/C stores pours, grind, ratio, and scale
behavior; the machine measures dose at use time. It cannot store coffee bypass, tea, xPod-native
geometry, J20 recipes, recipe names, notes, or citations. Every preset path rejects `bypass_ml`
instead of silently omitting it. Read `references/catalog.md` for the full representation boundary.

## Handle failures

- Runtime missing: run `scripts/bootstrap.py`; do not install packages globally or inside a
  read-only Skill cache.
- No machine found: confirm Bluetooth is on, the Agent is executing locally, the phone app is not
  holding the connection, and the machine is nearby. Treat Studio BLE as single-controller in
  practice: have the user fully close/disconnect the phone app before Agent BLE work.
- Multiple machines: select one with `--address` or `XBLOOM_ADDRESS`.
- Unknown firmware: stop. Only the deployment owner may use the explicit override documented in
  `references/device-safety.md` after controlled validation.
- Armed-state record exists: monitor or cancel; do not probe or load over it.
- Completion is unconfirmed: leave the state record intact and run `monitor` or `cancel`; monitor
  and cancel automatically reuse the recorded machine instead of scanning.
- Tea-loaded-state record exists: execute the unchanged recipe while currently ready, or cancel.
- Grinder cooldown active: wait for the reported remainder; never bypass the rest record.
- Bridge running: do not start a direct BLE command. Use `bridge status`/`bridge events`, use the
  matching bridge operation, or stop the idle daemon. If an activity is uncertain, keep the vessel
  in place and use `bridge cancel` or the machine's physical control before `bridge stop --force`.
- Bridge environment changed: restart the idle bridge; owner gates are captured by the daemon when
  it starts. Never force-stop an activity merely to reload configuration.
- `WAIT` or slow drawdown: follow the physical checks in `references/recipe-design.md`, then change
  one recipe variable.
- Interrupted or unsafe operation: run `cancel` and use the machine's physical control if BLE fails.

## Output contract

Match the user's language. Keep the response compact but include:

- Bean assumptions and flavor target.
- For coffee: dose, extraction water, bypass/final water where applicable, grind, RPM, and expected time.
- For tea: leaf mass, each steep's programmed water/temperature/pause, expected siphon output, and
  whether the recipe is official or adapted.
- Every pour: ml, temperature, pattern, vibration timing, pause, and flow.
- Validation result and file path.
- Cited public sources, source brewer, original device, match quality, and every adaptation when
  web enrichment was used. Keep citations outside executable YAML.
- Device state only if a BLE command was requested and actually run.
- For scale/grinder/water, report the requested values and confirmed completion/exit without
  publishing the machine identifier. Keep recipe target, cumulative machine output, and cup-scale
  increase separate; never describe any of them as water-supply inventory.
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
- Read `references/catalog.md` for authorized App/MMKV import, optional private sync, export, and
  A/B/C representation limits.
- Read `references/deployment.md` for Codex/Hermes installation, publication, and environment setup.
- Read `references/apk-capability-matrix.md` before claiming app parity or adding decoded commands.
- Read `references/sources.md` when checking provenance or making hardware/protocol claims.

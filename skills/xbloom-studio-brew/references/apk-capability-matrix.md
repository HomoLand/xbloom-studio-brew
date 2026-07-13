# Android APK capability matrix

This is the scope and parity ledger for the official xBloom Android app. Read it when deciding
whether a requested app feature belongs in this portable Skill, uses its long-lived local bridge,
or should remain in the vendor app.

## Audited snapshot and scope

The snapshot was retrieved from xBloom's public Android download and inspected for
interoperability on 2026-07-12:

| Property | Value |
| --- | --- |
| Package | `com.xbloom.tbdx` |
| Version | `2.2.2` (`versionCode 2002033`) |
| Minimum / target Android | 26 / 35 |
| SHA-256 | `29624db558917e6a975cd58a3123c240950d200a3adc4efe8ffef222e1b14c6e` |
| Studio device family | `J15` = xBloom Studio |
| Other device family in the same APK | `J20` = xBloom Original |

This Skill targets **J15/xBloom Studio only**. The APK also contains J20 Wi-Fi/cloud code, but that
is not a second control path for Studio and is not counted as missing Studio support. The APK and
decompiled source are not redistributed.

## Bottom line

The portable Skill covers the important Studio beverage path: coffee recipes, four-state
vibration and bypass, guarded start/cancel, FreeSolo scale/grinder/water, tea recipes, A/B/C
presets, persistent user settings, guarded mechanical tuning, structured machine telemetry, and
a private normalized import/sync path for official, user-created, Product/xPod, and Shared account
recipes, plus an explicitly gated add-only local-recipe upload. It does **not** claim literal app
parity.

Literal parity is neither possible nor desirable in one Agent Skill:

- Live pause/resume and in-cycle changes need one process to keep the BLE connection open. The
  bundled loopback bridge now owns and serializes that connection for coffee, tea, scale, grinder,
  FreeSolo water, presets, settings, and advanced tuning; direct one-shot BLE commands refuse
  while it runs.
- Login/account mutation, recipe edit/share/pin, history, store/cart, device sharing, cloud logs,
  and J20 remote control are mobile/cloud services rather than Studio BLE appliance commands. The
  Skill exposes only bounded own-account catalog reads and an idempotent add-only recipe subset;
  overwrite/delete/share/profile actions remain excluded.
- NFC is an Android tag reader that extracts an xPod identifier and asks xBloom's cloud for the
  recipe. The tag does not contain a complete portable brew program.
- Firmware flashing and calibration can leave the machine unusable. Knowing their commands is not
  sufficient evidence to expose them to a general-purpose Agent.

## Evidence labels

Availability and verification are different claims. This ledger uses four cumulative evidence
labels:

- **D — Decoded:** meaning comes from APK 2.2.2 or an attributed capture.
- **T — Deterministic:** builders, parser, safety behavior, and exact frames have automated tests.
- **H — Hardware-observed:** the command/workflow was exercised on a supervised Studio and the
  machine response or visible action was observed.
- **P — Physical-effect measured:** the resulting physical quantity was independently measured,
  such as outlet temperature. A BLE ACK, app report, or visible motion is not P evidence.

“Available” below means an Agent-facing guarded workflow exists; it does not upgrade its evidence
level. Where a row contains several controls, its boundary column identifies controls with weaker
evidence.

## User-visible capability coverage

| Area | What APK 2.2.2 can do for Studio | Skill availability | Evidence and boundary |
| --- | --- | --- | --- |
| Discovery and machine info | Connect over BLE; read model, firmware, units, water source, mode, and calibration baselines | **Available** through `scan`/`probe` and bridge; serial is redacted | **D/T/H**; more reports can still be modeled |
| Coffee recipe | Dose, grinder/no-grind, pours, patterns, four-state vibration timing, flow, pause, RPM, bypass, RT/BP temperatures | **Available** as guarded YAML | **D/T/H** for the retained capture-compatible program; legacy `agitation` is input-only and normalizes to `vibration` |
| Coffee execution | Load, arm, state-sensitive confirm/start, monitor, pause/resume, cancel, save A/B/C | **Available** one-shot and through the bridge | **D/T/H**; control-grade telemetry is not cloud app-history parity |
| Electronic scale | Enter, mandatory entry auto-zero, explicit re-tare, units, host timer, signed weight | **Available** one-shot and through the bridge | **D/T/H** for enter/read/re-tare/exit; persistent unit writes are only **D/T** |
| FreeSolo grinder | Size, RPM, start, pause, resume, stop, exit | **Available** one-shot and through the bridge with cooldown | **D/T** in this project ledger; motor actions remain excluded from unattended tests |
| FreeSolo brewer | Volume, RT/heated temperature, flow, circular/spiral/center pattern, tank/direct-feed source, pause/resume, live pattern/temp changes, stop, exit | **Available** for bounded dispense and bridge controls | Base dispense and running pattern switch are **D/T/H** on `V12.0D.500`; live temperature is **D/T**, not **P**; paused-state behavior is unmeasured; live volume/flow change is not exposed |
| Omni Tea Brewer | Upload dedicated tea program, execute on the same or a later connection, and receive soak/pause/restart/finish reports | **Available** one-shot and through the bridge | Dedicated frames/templates are **D/T**. Programmed 80/90 ml fills are distinct from approximate 120 ml finished output; report `40520` is a firmware-owned siphon finish, not configurable coffee bypass; phase reports do not imply intervention commands |
| Auto/Easy mode | Write the atomic A/B/C slot set, switch PRO/AUTO, and report order/count/mode | **Available** as one atomic A/B/C operation | **D/T/H** for the atomic batch and mode transition; command `11510` cannot represent bypass or tea, and arbitrary order/count editing is not public |
| Private recipe catalog | Fetch official coffee/tea, combined Studio-created, Product/xPod, Shared, current Easy, and default Easy records; add a local created recipe | **Available** for authorized JSON/decoded-MMKV import, ephemeral own-account reads, and preview-first add-only upload | Import, normalization, app-compatible RSA envelope, secret non-persistence, idempotency, and conflict refusal are **D/T**; all five default read categories were live-service verified on the owner's China tenant on 2026-07-14. Live-service evidence is not hardware **H**; the add endpoint is mock-tested and intentionally not live-mutated; no global-catalog claim |
| Units, display, water source | Set weight unit, temperature unit, display brightness, persistent water source | **Available** with readback/rollback one-shot and through the bridge | **D/T**; physical setting changes have not been supervised by this project |
| Pour radius and vibration amplitude | Read/write advanced mechanical tuning | **Available** with baseline-derived levels and rollback | **D/T**; APK UI ranges are enforced, physical writes remain unobserved |
| Grinder calibration | Drive the grinder to its zero/calibration position | **Excluded by default** | Service-grade motor action; vendor app/physical procedure should own it |
| Scale calibration / descaling | Guided physical maintenance screens | **Vendor-guided** | The Studio APK screens are mainly procedural, not a missing normal brew command |
| Firmware update | Download firmware, enter upgrade mode, transfer with YMODEM | **Excluded by default** | High bricking risk, signed-image/update-policy questions, and no recovery path in the Skill |
| NFC/xPod card | Read NfcV blocks, extract a six-character XID, fetch cloud recipe | **Reference input only** | Portable hosts may lack NFC; an authorized imported/fetched xPod record remains reference-only until explicit Omni adaptation |
| Account and cloud | Login/profile, browse/edit/share/pin recipes, history, device sharing, logs | **Catalog read plus add-only subset** | Login is ephemeral; official/created/Product/Shared reads persist only normalized recipes. Local coffee/tea upload is preview-first, add-only, and exact-confirmation gated. Profile, overwrite/delete, share/pin, history, logs, and credential extraction remain excluded |
| Store and content UI | Product catalog, cart, articles, notifications, app updates | **Not a machine-control feature** | Use the official app/site |
| xBloom Original (`J20`) | Wi-Fi/cloud machine control and J20-specific maintenance | **Out of scope** | It is a different product family in the same APK |

## Studio outbound command inventory

“Available” means an Agent-facing guarded workflow exists. “Decoded only” means the APK makes the
meaning clear but the Skill intentionally exposes no general command. Evidence labels retain the
definitions above.

### Session, coffee, and presets

| Command | APK meaning | Status |
| ---: | --- | --- |
| `8100` | Open app/session handshake | Available; **D/T/H** |
| `8022` | Back home / status handshake | Available; **D/T/H** |
| `8102` | Coffee bypass volume, bypass temperature, and dose | Available; **D/T/H**; disabled bypass preserves zero bytes |
| `8104` | Cup-geometry compatibility data | Internal fixed capture-compatible values; **D/T/H**; not a recipe temperature field |
| `8001` / `8004` | Send coffee recipe with grinder / no-grind | Available; **D/T/H** |
| `8002` | Commit loaded recipe | Available inside guarded start; **D/T/H** |
| `40518` | Confirm start only while freshly awaiting; pause while running | Available with state-sensitive dispatch; **D/T/H** |
| `40524` | Resume paused recipe | Available through bridge; **D/T/H** |
| `40519` | Stop/cancel recipe | Available; **D/T/H** |
| `8017` | Exit recipe pre-start screen | Available in unload/cancel recovery; **D/T/H** |
| `11510` | Write Easy/Auto recipe slot | Available only as atomic A/B/C programming; **D/T/H** |
| `11511` | Switch PRO/AUTO mode | Available only as part of slot programming; **D/T/H** |
| `11512` / `40525` / `11518` | Recipe order, count, and mode-state report | Decoded report only; no arbitrary order/count write API |

### FreeSolo and tea

| Command | APK meaning | Status |
| ---: | --- | --- |
| `8003` / `8500` / `8014` | Enter scale (auto-zero), explicit re-tare, exit | Available one-shot/bridge; **D/T/H** |
| `8006` / `3500` / `3505` / `8012` | Enter, start, stop, and exit grinder | Available one-shot/bridge; **D/T** |
| `8018` / `8020` | Pause/resume grinder | Available through bridge; **D/T** |
| `8007` / `4506` / `4507` / `8013` | Enter, start, explicit stop, and exit brewer | Available; **D/T/H**; STOP echo is `4507` |
| `8019` / `8021` | Pause/resume brewer | Available through bridge; **D/T/H** |
| `8016` | Change pattern during FreeSolo water | Available behind separate gate; **D/T/H** for running `center → spiral` on `V12.0D.500`; `8107` is optional |
| `4510` | Change temperature during FreeSolo water | Available behind separate gate; **D/T**, not **P**; optional `8108` is not thermometer proof |
| `4513` / `4512` | Upload and execute tea recipe | Available with load/execute separation; **D/T** |
| `40515` / `9012` / `9011` / `8113` | Tea pause/soak/restart/change-soak reports | Incoming machine reports, not equivalent app start controls |
| `40520` | Generic `bypass` work-mode report; tea uses it for post-soak finish/siphon | Decoded incoming report; tea semantics are distinct from command `8102` coffee bypass; **D/T** |

### Settings and service operations

| Command | APK meaning | Status |
| ---: | --- | --- |
| `8005` | Weight unit (`ml`, `g`, `oz`) | Available with `40521` readback/rollback; **D/T** |
| `8010` | Temperature unit (`C`, `F`) | Available with `40521` readback/rollback; **D/T** |
| `4508` | Persistent water source (`tank`, `tap`/direct-feed) | Available persistently and per dispense; **D/T** |
| `8103` | Display brightness (`1`, `8`, `15`) | Available with `40521` readback/rollback; **D/T** |
| `11506` / `11507` | Read/write pouring radius | Available as five levels derived from machine baseline; **D/T** |
| `11508` / `11509` | Read/write vibration amplitude | Available as six APK levels; **D/T** |
| `3502` | Grinder zero calibration | Excluded service action |
| `8101` | Enter firmware update, followed by YMODEM transfer | Excluded high-risk action |

## Incoming report coverage

The Skill decodes the reports needed for bounded operations and useful diagnostics:

- generic machine state and terminal/refusal states;
- signed standalone weights `10507`/`20501`;
- cumulative machine-dispensed water `40523`, natural brewer completion `40511`, and explicit STOP
  echo `4507`. `40523` is per-operation output, not water-supply inventory; cup weight comes separately
  from `20501`/low byte `0x15`;
- named grinder/brewer start, pause, resume/stop/exit reports plus brewer pattern `8107`, raw
  temperature-target report `8108` for bridge control state;
- model/firmware/settings report `40521`, combined settings report `8015`, advanced values
  `11506`-`11509`, pour stage, grinder size/speed, tea phase/soak time, Easy-mode raw state, xPod
  six-character XID, and named APK error reports.

The APK names still more progress/state reports whose payload semantics are only partially modeled.
Unknown payloads remain raw values rather than guessed application history. Full cloud brew-history
parity is therefore outside this local telemetry layer even when the underlying beverage action works.

## Important protocol uncertainties

Two findings retain explicit evidence boundaries:

1. The current encoder supplies fixed coffee command-`8104` compatibility values `110/90`,
   reproduced from an upstream capture and successful Studio hardware runs. This is intentionally
   absent from the public recipe schema: APK 2.2.2 labels the command as cup geometry and selects
   `80/40` for xPod or `90/40` for Omni/Other. The mismatch may be firmware/app evolution or a
   path-specific semantic difference. Keep the proven bytes until a controlled A/B capture on the
   target firmware establishes the correct migration; never present them as stage temperatures.
2. Persistent settings and type-2 mechanical tuning now use the exact APK values/frame builders,
   guarded UI ranges, read-after-write, and rollback. They have not been physically written by this
   project, so successful deterministic tests must not be described as a verified on-machine setting
   change. The four recipe-vibration wire values are independently modeled and byte-tested.

## Route to closer parity

The local long-lived BLE bridge is available: it owns one Studio connection, maintains a state
machine, serializes writes, and covers coffee, tea, scale, grinder, FreeSolo water, A/B/C presets,
persistent settings, advanced tuning, and bounded telemetry through a token-authenticated loopback
socket. Direct BLE commands refuse to race it.

The next evidence step is a supervised FreeSolo A/B for live target temperature, including measured
outlet lag. Paused-state pattern/temperature behavior and the other pattern transitions remain
separate evidence gaps; do not generalize the verified running `center → spiral` result beyond
firmware `V12.0D.500`. A later supervised pass can validate persistent settings and mechanical
levels one field at a time against the UI; until then their public status stays command-derived.
Tea intervention and deeper payload semantics remain separate follow-ups. The catalog read path now
has a controlled China-tenant compatibility check; international-tenant drift and future service
changes still require revalidation. The add form remains D/T until the owner deliberately chooses a
real recipe upload—release validation must never create disposable cloud records.
Calibration and OTA should remain a separate maintainer/service package even if their wire format
is completely decoded. Cloud/account and NFC remain optional integrations, never hidden
dependencies of the portable brewing Skill.

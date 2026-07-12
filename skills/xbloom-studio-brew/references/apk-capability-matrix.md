# Android APK capability matrix

This is the scope and parity ledger for the official xBloom Android app. Read it when deciding
whether a requested app feature belongs in this portable Skill, needs a long-lived local bridge,
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

The portable Skill covers the important Studio beverage path: coffee recipes and bypass, guarded
start/cancel, FreeSolo scale/grinder/water, tea recipes, A/B/C presets, machine state, and the core
machine-info report. It does **not** claim literal app parity.

Literal parity is neither possible nor desirable in one Agent Skill:

- Live pause/resume and in-cycle changes need one process to keep the BLE connection open. The
  current CLI intentionally performs one bounded operation per connection; a second CLI process
  cannot safely seize the machine mid-run.
- Accounts, recipe library/search/share, history, store/cart, device sharing, cloud logs, and J20
  remote control are mobile/cloud services rather than Studio BLE appliance commands.
- NFC is an Android tag reader that extracts an xPod identifier and asks xBloom's cloud for the
  recipe. The tag does not contain a complete portable brew program.
- Firmware flashing and calibration can leave the machine unusable. Knowing their commands is not
  sufficient evidence to expose them to a general-purpose Agent.

## User-visible capability coverage

| Area | What APK 2.2.2 can do for Studio | Skill status | Boundary / remaining work |
| --- | --- | --- | --- |
| Discovery and machine info | Connect over BLE; read model, firmware, units, water source, mode, and calibration baselines | **Shipped** through `scan`/`probe`; serial is redacted from normal output | More APK reports can be decoded for richer diagnostics |
| Coffee recipe | Dose, grinder/no-grind, pours, patterns, flow, pause, RPM, bypass, RT/BP temperatures | **Shipped** with guarded YAML; bypass and RT/BP are byte-tested | Four-state vibration timing is not yet modeled; see uncertainties below |
| Coffee execution | Load, arm, confirm/start, monitor, cancel, save A/B/C | **Shipped**, with load/start separation and physical gates | Live pause/resume needs a persistent bridge |
| Electronic scale | Enter, entry auto-zero, extra tare, units, timer in phone UI, signed weight | **Shipped** for enter/read/re-tare/exit; signed readings preserved | Persistent unit change is not exposed; timer is correctly host-side |
| FreeSolo grinder | Size, RPM, start, pause, resume, stop, exit | **Shipped** as a bounded start/stop/exit operation with cooldown | Interactive pause/resume needs a persistent bridge |
| FreeSolo brewer | Volume, RT/heated temperature, flow, pattern, tank/tap source, pause/resume, live pattern/temp changes, stop, exit | **Shipped** for bounded dispense/stop/exit, all temperatures, patterns, and both sources | Interactive pause/resume and live changes need a persistent bridge |
| Omni Tea Brewer | Upload dedicated tea program, execute, and receive soak/pause/restart reports | **Shipped** for separate load and guarded execute | In-cycle tea intervention is not exposed; five official templates are bundled |
| Auto/Easy mode | Write recipe slots/order/count and switch PRO/AUTO | **Shipped** for the hardware-verified atomic A/B/C batch and PRO/AUTO transition | Arbitrary order/count editing is not a separate public command |
| Units, display, water source | Set weight unit, temperature unit, display brightness, persistent water source | **Read-only** in `probe`; per-dispense water source is shipped | Persistent setting writes are protocol-known but need hardware/UI validation |
| Pour radius and vibration amplitude | Read/write advanced mechanical tuning | **Read-only baselines** in `probe` | Writes are intentionally not exposed without safe ranges and controlled hardware tests |
| Grinder calibration | Drive the grinder to its zero/calibration position | **Excluded by default** | Service-grade motor action; vendor app/physical procedure should own it |
| Scale calibration / descaling | Guided physical maintenance screens | **Vendor-guided** | The Studio APK screens are mainly procedural, not a missing normal brew command |
| Firmware update | Download firmware, enter upgrade mode, transfer with YMODEM | **Excluded by default** | High bricking risk, signed-image/update-policy questions, and no recovery path in the Skill |
| NFC/xPod card | Read NfcV blocks, extract a six-character XID, fetch cloud recipe | **Reference input only** | Portable hosts may lack NFC and account/cloud access; accept user/public recipe data instead |
| Account and cloud | Login/profile, browse/edit/share/pin recipes, history, device sharing, logs | **Out of portable BLE scope** | Would require a separately authenticated, policy-reviewed cloud connector |
| Store and content UI | Product catalog, cart, articles, notifications, app updates | **Not a machine-control feature** | Use the official app/site |
| xBloom Original (`J20`) | Wi-Fi/cloud machine control and J20-specific maintenance | **Out of scope** | It is a different product family in the same APK |

## Studio outbound command inventory

“Shipped” means an Agent-facing guarded workflow exists. “Decoded” means the APK makes the command
meaning clear, but this Skill does not expose a general command for it.

### Session, coffee, and presets

| Command | APK meaning | Status |
| ---: | --- | --- |
| `8100` | Open app/session handshake | Shipped |
| `8022` | Back home / status handshake | Shipped |
| `8102` | Coffee bypass volume, bypass temperature, and dose | Shipped; disabled bypass preserves old zero bytes |
| `8104` | Cup/staging geometry | Shipped with the hardware-captured compatibility values; see uncertainties |
| `8001` / `8004` | Send coffee recipe with grinder / no-grind | Shipped |
| `8002` | Commit loaded recipe | Shipped behind the start workflow |
| `40518` | Start while awaiting; pause while running | Start shipped; pause requires persistent session state |
| `40524` | Resume paused recipe | Decoded; persistent bridge required |
| `40519` | Stop/cancel recipe | Shipped |
| `8017` | Exit recipe pre-start screen | Shipped in unload/cancel recovery |
| `11510` | Write Easy/Auto recipe slot | Shipped as atomic A/B/C programming |
| `11511` | Switch PRO/AUTO mode | Shipped as part of slot programming |
| `11512` / `40525` / `11518` | Recipe order, count, and mode-state report | Decoded; no separate Agent workflow |

### FreeSolo and tea

| Command | APK meaning | Status |
| ---: | --- | --- |
| `8003` / `8500` / `8014` | Enter scale (auto-zero), explicit re-tare, exit | Shipped |
| `8006` / `3500` / `3505` / `8012` | Enter, start, stop, and exit grinder | Shipped |
| `8018` / `8020` | Pause/resume grinder | Decoded; persistent bridge required |
| `8007` / `4506` / `4507` / `8013` | Enter, start, stop, and exit brewer | Shipped |
| `8019` / `8021` | Pause/resume brewer | Decoded; persistent bridge required |
| `8016` / `4510` | Change pattern/temperature during FreeSolo water | Decoded; persistent bridge required |
| `4513` / `4512` | Upload and execute tea recipe | Shipped with load/execute separation |
| `40515` / `9012` / `9011` / `8113` | Tea pause/soak/restart/change-soak reports | Incoming machine reports, not equivalent app start controls |

### Settings and service operations

| Command | APK meaning | Status |
| ---: | --- | --- |
| `8005` | Weight unit (`ml`, `g`, `oz`) | Decoded; read-only through machine info |
| `8010` | Temperature unit (`C`, `F`) | Decoded; read-only through machine info |
| `4508` | Persistent water source (`tank`, `tap`) | Decoded; current setting is read and per-dispense source is supported |
| `8103` | Display brightness (`1`, `8`, `15`) | Decoded; read-only through machine info |
| `11506` / `11507` | Read/write pouring radius | Read baseline shipped; writes excluded pending range tests |
| `11508` / `11509` | Read/write vibration amplitude | Read baseline shipped; writes excluded pending range tests |
| `3502` | Grinder zero calibration | Excluded service action |
| `8101` | Enter firmware update, followed by YMODEM transfer | Excluded high-risk action |

## Incoming report coverage

The Skill fully decodes the reports needed for its bounded operations:

- generic machine state and terminal/refusal states;
- signed standalone weights `10507`/`20501`;
- metered water volume `40523` and brewer stop `40511`;
- model/firmware/settings report `40521`.

The APK also names richer progress, error, grinder, vibration, pod, Auto-mode, and tea reports,
including `40501`, `40502`, `40505`, `40507`, `40510`-`40513`, `40515`, `40517`, `40520`,
`40522`, `40526`, `40527`, `8203`, `8204`, `8111`, `9000`-`9012`, and `11518`. They remain raw
command acknowledgements unless a shipped workflow needs their payload. Full app-like diagnostic
telemetry is therefore a real remaining gap even when the underlying beverage action works.

## Important protocol uncertainties

Two findings should not be “fixed” from static decompilation alone:

1. The current Skill uses coffee command-`8104` values `110/90`, reproduced from an upstream
   capture and successful Studio hardware runs. APK 2.2.2 labels this command as cup geometry and
   selects `80/40` for xPod or `90/40` for Omni/Other. This may be firmware/app evolution or a
   path-specific semantic difference. Keep the proven bytes until a controlled A/B capture on the
   target firmware establishes the correct migration.
2. APK 2.2.2 presents four vibration timings: none, before, after, and both. The inherited recipe
   schema has only a legacy `agitation` boolean. Do not guess new wire values; capture all four
   options, add explicit schema values, and test every generated recipe frame before exposing them.

## Route to closer parity

The next architectural unit should be a small local **long-lived BLE bridge**, not more one-shot
commands. It would own the Studio connection, expose a session handle and state machine, serialize
writes, and offer pause/resume/live-adjust tools through MCP or a local socket. That unlocks the
interactive coffee, grinder, brewer, and tea controls without allowing two Agent processes to race
for BLE ownership.

After that, persistent settings can be added one by one with read-after-write and rollback tests.
Calibration and OTA should remain a separate maintainer/service package even if their wire format
is completely decoded. Cloud/account and NFC should likewise be optional platform integrations,
not hidden dependencies of the portable brewing Skill.

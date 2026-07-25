# xBloom App reverse map · path / status / error / gap audit

> **Kind:** analysis (no product implementation in this document)  
> **Frozen sources:** APK **2.2.2** / Studio **J15** matrix + in-repo core & Web SPA  
> **Date:** 2026-07-25  
> **Evidence policy:** D/T/H labels from `skills/xbloom-studio-brew/references/apk-capability-matrix.md`; Web findings labeled **static code**, **user-reported**, or **matrix**. No invented opcodes. No fresh APK decompile in this pass.

**Companion inventory (scratch greps / counts):** regenerate via  
`python {SCRATCH}/extract_inventory.py` → `inventory-summary.json`, `inventory-readable.txt`, `matrix-vs-web-cmds.txt`.

---

## 1. Operation-path inventory

Evidence column: **D** decoded APK · **T** deterministic tests · **H** hardware-observed (matrix) · **W** Web SPA static path · **U** user-reported · **?** unknown / partial payload.

### 1.1 Session / discovery / identity

| Path | App (matrix) | Skill / bridge | Web SPA | Evidence |
| --- | --- | --- | --- | --- |
| BLE scan / connect | App owns GATT | `scan` / bridge connect; sole BLE owner | Chrome Web Bluetooth + `getDevices` pair memory | D/T/H · W |
| Session open `8100` (a4) | On connect | `open_session` → a4 | `buildSessionStart` on connect (after fix) + tea/coffee load | D/T/H · W |
| Status handshake `8022` / `0x56` | After a4 | `request_status` / load settle | `buildStatusQuery` on connect + loads | D/T/H · W |
| Machine info report `40521` | Identity + units + mode baselines | `read_machine_info`; bridge waits on connect | `parseMachineInfoPayload` + `readMachineInfo`; SN cache / manual SN fallback | D/T/H · W · **U** (SN timeout class) |
| Release BLE for App | App reconnects | Workflow terminal → disconnect | Session disconnect after brew terminal; Tools keep link while used | D/T/H · W |

### 1.2 Coffee brew (outbound + UI)

| Step | Outbound cmd | Inbound / state | Skill | Web | Evidence |
| --- | ---: | --- | --- | --- | --- |
| Load program | `8100`,`8022`,`8102`,`8104`,`8001`/`8004` | `0x1d` loading → `0x1f` armed | `load` / bridge | `loadCoffee` / `buildLoadFrames` | D/T/H · W |
| Commit | `8002` | `0x1e` awaiting_confirm | state-sensitive start path | `writeCommit` / brew dialog | D/T/H · W |
| Confirm / start | `40518` | `0x22` starting → silent grind → `0x10`/`0x23`/`0x3b` brewing | start + monitor | `writeStart` + session phase | D/T/H · W |
| Pause / resume | `40518` pause · `40524` resume | paused reports | bridge | `pauseBrew` / `resumeBrew` | D/T/H · W |
| Cancel / quit pre-start | `40519` · `8017` | → idle / armed recovery | cancel / unload | cancel + quit helpers | D/T/H · W |
| Terminal | — | `0x24` ready (beep), later `0x01` idle cup-off; some FW `0x41` | terminal + history | terminal phase + local history | D/T/H · W |
| Live telemetry | — | `40523` water, `20501` cup, pour stage names | bridge stream | parseNotification subset | D/T/H · W |

### 1.3 Tea (Omni Tea Brewer)

| Step | Cmd / report | Skill | Web | Evidence |
| --- | --- | --- | --- | --- |
| Set cup geometry | `8104` | tea load | `buildTeaSetCup` | D/T · W |
| Upload tea code | `4513` | tea-load | `buildTeaLoadFrames` | D/T · W |
| Execute | `4512` | tea-start | `startTea` | D/T · W |
| Soak / pause / restart | reports `9012`,`40515`,`9011`,`8113`; state **`0x45` tea_soaking** (Web) | decode + phase | Web session treats `0x45` as active brew | D/T · W (state named on Web; **not** in core `STATE_NAMES`) |
| Siphon finish | report `40520` (not coffee bypass `8102`) | named report | named in `REPORT_NAMES` | D/T · W |

### 1.4 FreeSolo (scale / grinder / water)

| Subsystem | App cmds | Skill | Web Tools UI | Evidence |
| --- | --- | --- | --- | --- |
| Scale enter / tare / exit | `8003`,`8500`,`8014` | one-shot + bridge | Tools | D/T/H · W |
| Scale weight stream | `10507` / `20501` | decode signed g | decode | D/T/H · W |
| Grinder enter/start/stop/quit | `8006`,`3500`,`3505`,`8012` | available | Tools (no pause/resume UI) | D/T · W partial |
| Grinder pause/resume | `8018`,`8020` | bridge | **no builder** on Web | D/T · gap |
| Brewer enter/start/stop/quit | `8007`,`4506`,`4507`,`8013` | available | Tools bounded water | D/T/H · W |
| Brewer pause/resume | `8019`,`8021` | bridge | **no builder** on Web | D/T/H · gap |
| Live pattern / temp | `8016`,`4510` (+ reports `8107`,`8108`) | gated | **missing** | D/T/H pattern; temp D/T not P · gap |

### 1.5 Easy / Auto A/B/C

| Path | Wire / cloud | Skill | Web | Evidence |
| --- | --- | --- | --- | --- |
| Write A/B/C batch | `11511` PRO → `11510`×3 → AUTO; states `0x43`→`0x25` | `save-slots` atomic | `saveSlots` / SlotProgrammer write | D/T/H · W |
| Mode / order / count reports | `11518`,`11512`,`40525` | decode only; no arbitrary write API | name-only for `11518` | D · partial |
| **Read current slots** | **Not** full BLE recipe dump; App uses account Easy list + SN | catalog `easy` / `easy-default` with `sn`,`countryId`,`tableId` | `fetchEasyModeSlotsFromCloud` + BLE SN (`40521`) | D/T catalog · W · **U** (40521 fail) |
| Scale flag per slot | scale 1/2 in Easy metadata | catalog merge | parse `scale` in `easyModeSlots` | D/T · W |

### 1.6 Settings / service (BLE)

| Path | Cmd | Skill | Web | Evidence |
| --- | ---: | --- | --- | --- |
| Weight / temp unit, display, water source | `8005`,`8010`,`8103`,`4508` + `40521` readback | available + rollback | **missing builders/UI** | D/T · gap |
| Pour radius / vibration amplitude | `11506`–`11509` | available | **missing** | D/T · gap |
| Grinder zero cal | `3502` | **excluded** | excluded | intentional |
| OTA / YMODEM | `8101` | **excluded** | excluded | intentional |

### 1.7 Cloud / account / catalog / share / history

| Path | Endpoint (App RSA envelope) | Skill | Web | Evidence |
| --- | --- | --- | --- | --- |
| Login | `tMemberLogin.thtml` | ephemeral env login | Settings credentials + sessionStorage password | D/T live China 2026-07-14 · W |
| Official coffee / tea | `tHostRecipe.thtml` / `tuTeaRecipe.tuhtml` | catalog sync | Recipes sync → 官方 | D/T · W |
| Created | `tuMyTeaRecipeCreated.tuhtml` | sync + push add-only + delete | push / update=delete+create / delete owned | D/T · W |
| Product / shared | `tuMyRecipeProduct` / `tuMyRecipeShared` | sync | sync → product / 分享 | D/T · W |
| Easy list / default | `tuEasyModeList` / `tuEasyModeInitList` | sync when `easy_mode` config | Easy read path | D/T · W |
| Recipe add / delete | `tuRecipeAdd` / `tuRecipeDelete` | preview + confirm phrases | owned cloud only | D/T · W |
| Brew history | `tuBrewRecordList.tuhtml` | history-sync | History import | D/T · W |
| Public share H5 | `RecipeDetail.html` + tableIdOfRSA | not primary Skill path | import share link local-only; create share probes | W · static |
| Share/pin/profile mutation | App mobile | **excluded** | partial create-share probe only | intentional / partial |
| NFC xPod XID → cloud | Android NfcV | reference import only | **missing** | intentional platform |
| Store / cart / J20 Wi-Fi | App | **out of scope** | out of scope | intentional |

### 1.8 Major App UI flows (not full screen parity)

| Flow | Skill surface | Web surface | Notes |
| --- | --- | --- | --- |
| Coffee brew | CLI + bridge | Recipes + BrewConfirm + Dashboard Live View | Web uses Web Bluetooth or bridge driver |
| Tea brew | CLI + bridge | Recipes tea path | Confirm dialog; Omni basket |
| FreeSolo | CLI + bridge | Tools page | Subset of controls |
| Easy A/B/C | `save-slots` + catalog Easy | SlotProgrammer read/write | Read = cloud+SN not pure BLE |
| Catalog / share / history | catalog CLI | Recipes / Settings / History | Ownership rules Web-specific |
| Settings machine units | bridge settings | account/driver/AI only | **no** unit write UI on Web |
| Design / AI recipe | knowledge + design | Design page + AI prefs | Product path, not APK BLE |

---

## 2. State & status-code map

### 2.1 Sources

| Artifact | Path |
| --- | --- |
| Core states / reports | `packages/core/xbloom_ble/telemetry.py` — `STATE_NAMES`, `REPORT_NAMES`, `IGNORED_STATES`, `TERMINAL_STATES` |
| Web mirror | `xbloom-studio-web/frontend/src/ble/telemetry.ts` — same names + **extra** state/report rows |
| Outbound inventory | `skills/xbloom-studio-brew/references/apk-capability-matrix.md` § Studio outbound / incoming |

Inventory run (2026-07-25): **core 14 states, 42 REPORT_NAMES**; Web **15 states, 46 REPORT_NAMES**.

### 2.2 Machine state bytes (`STATUS_COMMAND` 8023 / `0x57` envelope, marker `0xC1`)

| Byte | Name | Role | Core | Web | Semantics |
| ---: | --- | --- | :---: | :---: | --- |
| `0x01` | idle | At rest; after cup lift post-brew | ✓ | ✓ | Core `TERMINAL_STATES` includes idle; Web **excludes** idle from always-terminal — session only terminals idle after `brewActed` |
| `0x0c` | no_water | Pre-grind water fault | ✓ | ✓ | Active fault / stop path |
| `0x0f` | no_beans | Waiting for beans | ✓ | ✓ | Waits; cancel possible |
| `0x10` | brewing | Live pour | ✓ | ✓ | Active; **silent grind** may precede after `0x22` (matrix/core comment) |
| `0x1d` | loading | Recipe staging | ✓ | ✓ | Pre-armed |
| `0x1e` | awaiting_confirm | Human red confirm | ✓ | ✓ | Commit landed |
| `0x1f` | armed | Ready for commit/start | ✓ | ✓ | Load success |
| `0x22` | starting | Post-confirm spin-up / grind | ✓ | ✓ | Active; grind may be silent |
| `0x23` | brewing | Mid-pour sub-state | ✓ | ✓ | Active |
| `0x24` | ready | Coffee ready **beep** (true brew end) | ✓ | ✓ | Terminal (both) |
| `0x25` | slots_saved | Easy write done | ✓ | ✓ | Slot save success |
| `0x3b` | brewing | Another pour sub-state | ✓ | ✓ | Active |
| `0x41` | complete | Alt complete | ✓ | ✓ | Terminal |
| `0x43` | saving_slots | Easy write in progress | ✓ | ✓ | Batch save |
| `0x45` | tea_soaking | Mid-tea steep / inter-steep | **not in core STATE_NAMES** | ✓ Web-only name | Web `isActedBrewState`; avoids unknown_0x45 UX |
| `0x15`,`0x4b` | (ignored as *states*) | Heartbeat mis-decode of cup/water low bytes | IGNORED | IGNORED | Never drive phase |

**Web not missing any core `STATE_NAMES` row.** Core does not mirror Web’s `0x45` label (reports `9012` cover tea soak by command instead).

### 2.3 Named report / command codes (`REPORT_NAMES` + key constants)

Every entry in core `REPORT_NAMES` (42). Web mirrors each name; Web adds four extras (below).

| Code | Name | Notes |
| ---: | --- | --- |
| 8009 | machine_sleeping | |
| 8011 | machine_awake | |
| 8015 | settings_changed | Structured units payload in core parser |
| 8023 | machine_activity | Status envelope |
| 8105 | grinder_size | |
| 8106 | grinder_speed | |
| 8111 | easy_mode_begin | |
| 8113 | tea_soak_time_changed | |
| 8203 | abnormal_gear_position | ERROR_REPORTS |
| 8204 | abnormal_dose_or_water | ERROR_REPORTS |
| 9000 | grinder_state | FreeSolo grinder |
| 9001 | brewer_state | FreeSolo brewer |
| 9002 | machine_state | |
| 9003 | grinder_started | |
| 9004 | grinder_exited | |
| 9005 | brewer_started | |
| 9006 | brewer_exited | |
| 9008 | machine_state | duplicate name family |
| 9009 | grinder_paused | Skill has pause builder; Web Tools missing |
| 9010 | brewer_paused | Skill has pause builder; Web Tools missing |
| 9011 | tea_restarted | |
| 9012 | tea_soaking | Report-level soak (state `0x45` named only on Web) |
| 11506 | pour_radius | Read report / Skill settings |
| 11507 | pour_radius_written | |
| 11508 | vibration_amplitude | |
| 11509 | vibration_amplitude_written | |
| 11518 | easy_mode_state | Mode report, not full recipe blob |
| 40501 | xpod_detected | NFC-related report |
| 40502 | coffee_brewer_started | |
| 40505 | gear_position | |
| 40507 | grinder_stopped | |
| 40510 | pour_stage | |
| 40511 | brewer_stopped | Natural completion related |
| 40512 | brew_ready | |
| 40513 | brew_ready_alt | |
| 40515 | tea_paused | |
| 40517 | idle_grinding_error | ERROR_REPORTS |
| 40520 | bypass_started | Tea siphon finish ≠ coffee `8102` |
| 40522 | no_water_report | ERROR_REPORTS |
| 40523 | water_volume | Cumulative ml |
| 40526 | current_grinder | |
| 40527 | vibration_before_pour | |

**Not in `REPORT_NAMES` dict but first-class:** **40521** `machine_info` (constant + payload parser; event `state_name="machine_info"`).

**Web-only extras in Web `REPORT_NAMES` (core handles via dedicated constants / branches):**

| Code | Web name | Core handling |
| ---: | --- | --- |
| 8107 | brewer_pattern | `BREWER_MODE_COMMAND` decode |
| 8108 | brewer_temperature | `BREWER_TEMPERATURE_COMMAND` |
| 10507 | scale_weight | `SCALE_WEIGHT_COMMAND` |
| 20501 | cup_weight | `CUP_WEIGHT_COMMAND` |

### 2.4 Terminal vs active vs ignored (summary)

| Class | Codes / rule |
| --- | --- |
| **Ignored state bytes** | `0x15`, `0x4b` — never phase |
| **Always brew-terminal (Web)** | `0x24`, `0x41` |
| **Always terminal set (core constant)** | `0x24`, `0x41`, **`0x01`** — consumers must still avoid idle-at-rest false end |
| **Active brew (Web `isActedBrewState`)** | `0x22`,`0x10`,`0x23`,`0x3b`,`0x45`,`0x0c`,`0x0f` |
| **Armed** | `0x1f` |
| **Slot save** | `0x43` → `0x25` |

### 2.5 Multi-step sequences (verified patterns)

```text
Coffee:  a4 + 0x56 settle → load(a6,a8,pours) → 0x1d → 0x1f armed
         → commit 8002 → 0x1e awaiting_confirm
         → 40518 start → 0x22 starting → (silent grind) → 0x10/0x23/0x3b brewing
         → 0x24 ready (beep) → [cup off] 0x01 idle

Tea:     a4 + 0x56 settle → 8104 + 4513 → 4512 start
         → reports 9012/0x45 soak, 40515 pause, 9011 restart, 40520 siphon finish
         → terminal ready/idle family

Easy write: 11511 PRO → 11510 A,B,C → (0x43 saving) → 0x25 saved → AUTO

Machine info: a4 → 0x56 → notify 40521 (serial/firmware/units). Status alone may omit 40521 (U/W).
```

### 2.6 Outbound command spot-check (matrix → stacks)

Matrix lists **58** numeric command/report ids. Web **sends ~28** beverage/FreeSolo/session builders; **decodes** water/cup/scale/40521/tea reports. **WEB_MISSING** (no Web builder/UI; Skill may still have): includes settings `8005/8010/8103/4508`, advanced `11506–11509`, FreeSolo pause/resume `8018–8021`, live `8016/4510`, cal `3502`, OTA `8101`, order/count `11512/40525`, etc. Full line list: scratch `matrix-vs-web-cmds.txt`.

**Partial payload semantics (explicit gaps):** many progress reports only named; advanced settings physically unwritten (matrix D/T not H); `8104` fixed `110/90` vs App xPod/Omni `80/40`/`90/40` uncertainty remains open (matrix §Important protocol uncertainties).

---

## 3. Error & mismatch audit

Each row: **bug** vs **intentional exclusion** vs **platform limit**.

| # | Symptom | Likely root cause | Stack | Class | Evidence |
| --- | --- | --- | --- | --- | --- |
| **E1** | Easy **读取当前档位** hangs then `Machine did not return serial (40521)` | Web requested status without reliable **a4→0x56** handshake; some firmwares only emit 40521 after session open; long frame reassembly/timeout risk | **Web** | **Bug** (mitigated: connect a4, stronger `readMachineInfo`, manual/cached SN) | **U** user error text; **static** `session.readMachineInfo` / matrix open_session |
| **E2** | Share import treated as owned cloud / delete fails `not_on_account` | Share `recipe_id` / author tableId mistaken for member ownership | **Web** | **Bug** (addressed: local-only import, `isOwnedOnCloud`, heal `cloud:region:id`) | **static** `localRecipes` / Recipes.tsx history of fixes |
| **E3** | After cloud delete, UI **Recipe not found** | `runCloud` re-opened deleted `selectedId` | **Web** | **Bug** (quietMissing + clear selection) | **static** Recipes.tsx |
| **E4** | Dual cards 分享+我的 after import→upload→sync | Same recipe under share id and cloud id | **Web** | **Bug** (promote + prune) | **static** `promoteLocalToCloudOwned` / `pruneRedundantShareCopies` |
| **E5** | Tea brew UI **unknown_0x45** / early disconnect | `0x45` not named; idle treated terminal before tea acted | **Web** | **Bug** (0x45 named; brewActed gate) | **static** telemetry + session; **U** prior session |
| **E6** | Fake “官方” seeds / wrong 官方 label | Bundled starters or mis-classified catalog targets | **Web** | **Bug** (seeds removed; host only = 官方) | **static** localRecipes tests |
| **E7** | Silent grind after `0x22` looks “stuck” | Firmware grinds without intermediate state | App/Skill/Web | **Protocol semantic** (not a code bug if UI waits for `0x10`) | matrix/core comment **D/H** |
| **E8** | `8104` geometry bytes ≠ App xPod/Omni table | Capture-compatible fixed values kept intentionally | Skill+Web | **Known uncertainty** | matrix §uncertainties **D/T/H** vs App tables |
| **E9** | CORS / browser blocks cloud RSA APIs on GH Pages | Browser origin vs App TLS client | **Web** | **Platform limit** | static cloud client notes |
| **E10** | Settings / advanced tuning / FreeSolo pause-resume absent on Web | Not ported builders/UI | **Web** | **Gap** (Skill has many) | matrix + `matrix-vs-web-cmds.txt` |
| **E11** | Easy **read** is cloud+SN not live machine dump | BLE never exposes full slot recipe blob publicly | All | **Protocol limit** (App same cloud path) | catalog.md; matrix Auto/Easy |
| **E12** | OTA / grinder cal / J20 / NFC full path | High risk or non-Studio / no NFC host | All | **Intentional exclusion** | matrix |
| **E13** | Core `TERMINAL_STATES` includes `0x01`; naive consumers end at connect-idle | Documented pitfall | Skill consumers | **Semantic risk** | telemetry.py comments **T** |
| **E14** | Web FreeSolo Tools vs Skill bridge concurrent | Two BLE owners if bridge + Web both live | Web+Skill | **Architecture risk** | ADR / BLE-LIFECYCLE |

---

## 4. Capability gap matrix

Legend: **Avail** full guarded path · **Partial** subset / alternate path · **Missing** no usable path · **Excl.** intentional.

| Capability | Official App | Skill (Python/bridge) | Web SPA | Why / note |
| --- | :---: | :---: | :---: | --- |
| Connect / session / status | Avail | Avail | Avail | Web: WebBT + a4/0x56 |
| Machine info / SN | Avail | Avail | **Partial** | Decode+probe; **U** 40521 flaky → manual SN |
| Coffee load / start / cancel | Avail | Avail | Avail | Goldens aligned |
| Coffee pause / resume | Avail | Avail | Avail | 40518/40524 |
| Tea load / start | Avail | Avail | Avail | D/T; soak UX improved |
| Tea deep intervention | Partial App | Partial decode | Partial | Reports ≠ full controls |
| FreeSolo scale | Avail | Avail | Avail | Tools |
| FreeSolo grinder start/stop | Avail | Avail | **Partial** | No pause/resume on Web |
| FreeSolo water dispense | Avail | Avail | **Partial** | No live pattern/temp change |
| Easy **write** A/B/C | Avail | Avail | Avail | Atomic batch |
| Easy **read** A/B/C | Avail (UI+cloud+SN) | Partial (catalog easy targets) | **Partial** | Cloud+SN; needs account+serial |
| Persistent settings units/display | Avail | Avail (D/T) | **Missing** | Not ported to Web |
| Advanced radius/amplitude | Avail | Avail (D/T) | **Missing** | Not ported |
| Catalog sync 5 targets | Avail | Avail | Avail | + easy endpoints on demand |
| Catalog push / delete owned | Avail edit | Add-only + delete | Push/update/delete owned | Web update = delete+create |
| Share import / create | Avail | Limited | **Partial** | Import H5; create probes; local until upload |
| Brew history import | Avail | Avail | Avail | Coarser than BLE journal |
| Local BLE brew journal | n/a App cloud | Avail | Avail | Web localStorage journal |
| Live View / telemetry charts | App UI | CLI/events | **Partial** | Dashboard samples |
| NFC / xPod card read | Avail | Reference only | **Missing** | No browser NFC path |
| OTA firmware | Avail | **Excl.** | **Excl.** | Brick risk |
| Grinder calibration | Avail | **Excl.** | **Excl.** | Service motor |
| J20 Original | Separate family | **Excl.** | **Excl.** | Out of scope |
| Store / social / pin | Avail | **Excl.** | **Excl.** | Not appliance control |
| Design AI / photo bean | n/a | knowledge CLI | Avail (product) | Beyond APK matrix |
| Home Assistant thin client | n/a | Roadmap F | n/a | docs/ROADMAP-NEXT |

**Required AC rows called out:**

| Area | App | Skill | Web | Cite |
| --- | --- | --- | --- | --- |
| Coffee load/start | Avail | Avail | Avail | matrix coffee · `load.ts` / bridge |
| Tea load/start | Avail | Avail | Avail | matrix tea · `teaLoad.ts` |
| Easy write | Avail | Avail | Avail | `11510` batch · slots.ts |
| Easy read | Avail | Partial catalog | Partial cloud+SN | E1 **U** 40521; `easyModeSlots.ts` |
| FreeSolo scale | Avail | Avail | Avail | Tools · 8003/8500/8014 |
| Catalog sync | Avail | Avail | Avail | ENDPOINTS five + easy |
| Share import | Avail | Limited | Partial local H5 | `importShareLink` ownership rules |

---

## 5. Development directions (prioritized, non-binding)

### Near-term fix (reliability / honesty)

1. **Harden 40521 / SN path on Web** — treat a4+status as mandatory; surface SN on connect; keep manual SN; log undecoded 40521; optional longer MTU diagnostics.  
2. **Close remaining cloud ownership edge cases** — share create endpoint stability; CORS-aware error copy; never show 官方 for non-host.  
3. **Align tea state model core↔Web** — consider adding `0x45` to core `STATE_NAMES` or document single source of truth.  
4. **Document silent-grind UX** — status copy after start so “no state change” is expected until pour.

### Medium parity (Web ↔ Skill)

5. **Port FreeSolo pause/resume** (grinder `8018/8020`, brewer `8019/8021`) to Web Tools.  
6. **Port persistent settings + 40521 readback** (units, display, water source) behind explicit confirm.  
7. **Optional advanced tuning** UI (radius/amplitude) read-only first, write later with rollback.  
8. **Bridge driver path completion** for Web when WebBT unavailable (LAN pairing already in architecture docs).  
9. **Easy read offline cache** — store last cloud snapshot keyed by SN; show staleness.

### Long-term research

10. **Controlled `8104` A/B capture** on target firmware (xPod vs Omni tables) before changing goldens.  
11. **P-grade FreeSolo temperature lag** measurement (matrix next evidence step).  
12. **Deeper report payload models** only where UI needs them (pour_stage, easy_mode_state fields).  
13. **NFC host strategy** (native helper vs out-of-band XID paste) — not pure browser.  
14. **Home Assistant thin client** (ROADMAP-NEXT F) reusing bridge — not a second BLE stack.  
15. Keep **OTA / calibration / J20** out of general Agent/Web surfaces.

---

## Risks (this audit)

- No live APK re-decompile or new supervised BLE campaign in this environment; H labels are from the existing matrix/hardware-validation ledger; Web 40521 failure is **user-reported** + static.  
- “全面逆向” frozen to **structured inventory + gap audit from in-repo evidence**, not exhaustive APK string RE.  
- Web vs Skill divergence is often **intentional** (platform, safety); listed as Excl./Partial, not automatically “bug”.

---

## Appendix C — Follow-up implementation notes (2026-07-25+)

| Item | Status |
| --- | --- |
| Core `STATE_NAMES[0x45]=tea_soaking` | Aligned with Web (was Web-only) |
| FreeSolo live pattern/temp (`8016`/`4510`) | Web Tools builders + UI |
| Pour radius / vibration amplitude | Web Tools advanced R/W (D/T frames) |
| Recipes Bridge brew | Button enabled; local recipes upserted to host SQLite then `coffee/tea.load` |
| `8104` coffee geometry | Still fixed `110/90`; documented in `load.ts` + matrix — A/B not done |

**8104 research remaining:** supervised capture comparing App Omni/xPod tables vs current goldens on target firmware before changing `COFFEE_CUP_GEOMETRY_COMPAT`.

---

## Appendix A — File index used

| Area | Paths |
| --- | --- |
| Matrix | `skills/xbloom-studio-brew/references/apk-capability-matrix.md` |
| Catalog | `skills/xbloom-studio-brew/references/catalog.md`, `packages/core/xbloom_catalog.py` |
| Protocol / telemetry | `packages/core/xbloom_ble/{protocol,telemetry,client,bridge,tea}.py` |
| Web BLE | `xbloom-studio-web/frontend/src/ble/*` |
| Web cloud / Easy | `lib/xbloomCloud.ts`, `easyModeSlots.ts`, `cloudCatalog.ts`, `localRecipes.ts` |
| Roadmaps | `docs/ARCHITECTURE-AND-ROADMAP.md`, `ROADMAP-NEXT.md`, `BLE-LIFECYCLE.md`, `ADR-WEB-BLUETOOTH.md` |

## Appendix B — Inventory counts (reproducible)

```
core STATE_NAMES: 14
web  STATE_NAMES: 15  (+0x45 tea_soaking)
core REPORT_NAMES: 42
web  REPORT_NAMES: 46  (+8107,8108,10507,20501)
matrix numeric ids: 58
web ble builders (export function build*): 32
skill protocol build_* functions: 46
WEB_MISSING matrix ids (no Web send/decode mapping in audit script): 23
```

Scratch artifacts: `inventory-summary.json`, `inventory-readable.txt`, `matrix-vs-web-cmds.txt`, `extract_inventory.py`.

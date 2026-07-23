# ADR: Progressive pure Web + Web Bluetooth

> Status: **Accepted** (2026-07-24); **W0–W4 implemented** (2026-07-24)  
> Scope: `xbloom-studio-web` progressive path; brew core remains protocol source of truth  
> Related: `ARCHITECTURE-AND-ROADMAP.md` ADR-12 (LAN), Skill/HA bridge thin clients

---

## Context

Users want an App-like experience: on a **Chrome device that has BLE and is near the Studio**, open our website and control the machine **without** installing a local Python bridge/runtime.

Constraints agreed for v1 of this path:

| Decision | Choice |
|----------|--------|
| Browser | **Chrome only** (desktop + Android) |
| Transport | **Web Bluetooth** (browser GATT), not cloud relay |
| Local bridge | Optional **legacy** path during migration |
| Tab close / background recovery | **Out of scope** |
| Multi-client locking with Skill/HA | **Out of scope** (product: do not brew on two clients at once; Skill/HA release after terminal) |
| Approach | **Progressive** in existing web frontend (`driver: bridge \| web-bluetooth`) |

Physics reminder: Web Bluetooth uses the **opening device’s** radio. This is near-field only, which matches the product intent.

---

## Decision

1. Add a **machine driver** abstraction in the web frontend:
   - `bridge` — existing HTTP → local `xbloom-bridge` (legacy / advanced)
   - `web-bluetooth` — Chrome Web Bluetooth + TypeScript protocol port (**default when usable**)
2. Port wire protocol to TypeScript (`framing` → load → start/monitor), with **golden hex vectors** against `packages/core/xbloom_ble/protocol.py`.
3. Host as static SPA when web-bluetooth is primary; backend remains for AI design / LAN bridge mode.
4. Do **not** remove Skill/HA/bridge; they stay for Agent and Home Assistant.

### Non-goals (this ADR)

- iOS Safari Web Bluetooth
- Remote control without a BLE-capable browser near the machine
- Sharing `state.db` between browser and bridge on day one
- Equal durability to daemon (page lifetime = session lifetime)
- **W5** full cloud AI design offline of backend (optional later)

---

## Architecture

```text
                    ┌─ driver=bridge ──────────► HTTP API ► xbloom-bridge ► BLE
Browser SPA ───────┤
                    └─ driver=web-bluetooth ──► Web Bluetooth GATT ──────► BLE
```

Preference stored in `localStorage` (`xbloom.machineDriver`).  
Default: `web-bluetooth` when `detectWebBluetooth().usable`, else `bridge`.

### Module layout (frontend)

```text
src/machine/     # driver preference + React context
src/ble/
  constants.ts   # GATT UUIDs (aligned with core client.py)
  framing.ts     # CRC-16/KERMIT, xbloom_frame, j15_frame
  load.ts        # buildLoadFrames (coffee)
  telemetry.ts   # parseNotification + frame stream
  gatt.ts        # requestDevice / connect / write / notify
  session.ts     # connect → load → start → cancel → terminal disconnect
  coffeeRecipe.ts
  capabilities.ts
```

### Protocol source of truth

- Python `xbloom_ble.protocol` / `telemetry` remain canonical.
- TS matches byte-exact frames for commit/start/cancel and coffee load sequence.

---

## Implementation phases

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **W0** | Driver flag, capabilities, framing golden tests, GATT connect | **Done** |
| **W1** | Notify decode + live phase/scale in UI | **Done** |
| **W2** | Coffee load frames + armed wait; no auto-start | **Done** |
| **W3** | Start (confirm + phrase), cancel, monitor terminal, GATT disconnect | **Done** |
| **W4** | Default driver = web-bluetooth when capable | **Done** |
| **W5** | Optional cloud AI without backend | **Deferred (non-goal)** |

---

## Safety

- Load never auto-starts (load frames exclude commit/start/cancel).
- Start requires exact ready phrase (`cup-filter-water-beans` for coffee) in UI before load+start on web-bluetooth.
- Product copy: close official App while using Web; do not run Skill/HA brew in parallel.

---

## Acceptance checklist

- [x] This ADR
- [x] `machine` driver preference + Settings panel
- [x] `ble/framing` + golden tests vs Python vectors
- [x] `ble/gatt` + `session` connect/disconnect
- [x] W1 notify decode + UI live state
- [x] W2 coffee load + golden load frames
- [x] W3 start/cancel/monitor/release + phrase gate
- [x] W4 default driver when Web Bluetooth usable
- [x] Local brew history + full-session telemetry (browser localStorage)
- [x] FreeSolo basics: grinder / free hot water / electronic scale
- [x] Official account catalog: sync / add / delete / update (delete-then-create)
- [ ] W5 (optional, deferred)

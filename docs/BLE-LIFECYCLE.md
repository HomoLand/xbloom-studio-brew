# BLE connection lifecycle (implemented slice)

Status: implemented in `packages/core/xbloom_ble/bridge.py` + `client.py` + `xbloom_storage.StateStore` with fake-client tests.

**Implemented Phase A core slice:** A1–A2, **A3 complete** (core + Skill + Web/MCP session/client exit: HTTP/page/client disconnect must not cancel or release the daemon-owned durable workflow; passive `status`/`events` never mutate BLE), A4–A5, **A6** (unexpected BLE disconnect + `recovery.reconcile` + external busy), A7, A8, **A9 complete** (core + Skill + Web + MCP via shared `TypedBridgeClient` after sibling Web commit `63d91a4`; Skill CLI all active hardware via daemon; Web typed routes/MCP same typed client contract; only passive scan uses discovery), **A10 complete** (core unit matrix + real JSON-line multi-client transport tests in `test_a10_transport_integration.py`: cross-client handoff/exit, concurrent start same/distinct `request_id`, daemon reconstruction over transport).

**Still not full Phase A:** only **A11** real-hardware validation remains (see also `skills/xbloom-studio-brew/references/hardware-validation.md` H00–H08). Code/tests for A1–A10 are complete on `codex/roadmap-completion` / core v1.2.0.

## Rules in force

| Situation | BLE behavior |
|---|---|
| Daemon / `BridgeCore` construction, `status`, `events` | No connect; status/events never arm, reset, or extend the idle timer |
| Skill / Web / MCP `TypedBridgeClient` | Shared typed API; `client_name` is diagnostic/visibility only (not authorization; Web MCP adapter may share e.g. `xbloom-studio-web` with Web and does not uniquely distinguish Skill/Web/MCP); mutating RPCs get `request_id` (caller may supply); workflow-bound methods require `workflow_id`; hardware ensures daemon; status/events never connect or mutate BLE; no auto-retry of uncertain ops; HTTP/page/client process exit does not cancel or release daemon-owned durable workflows |
| `probe` RPC | One-shot connect via BridgeCore; redacted machine info; prompt-release auto-owned; retain explicit debug link; never direct Bleak from CLI |
| Explicit `connect` RPC | Connect; `connection_scope=explicit`; hold until explicit `disconnect`; **never** auto-released or idle-timed-out; upgrading an existing auto-owned orphan immediately clears idle timeout fields/task |
| Coffee / tea `load` | Create durable workflow + snapshot **before** BLE load write; connect if needed (`workflow`); return `workflow_id`; reuse connection through start/pause/resume/events/terminal |
| Coffee / tea still `loaded` (awaiting start) | Hold the workflow connection; wait for start or explicit cancel; **no** time-based cancel, unload, expiry, five-minute loaded timeout, or disconnect |
| Grinder / water / scale start | Durable one-shot `workflow_id`; connect if needed (`one-shot`); reuse until that op ends |
| `settings.write` / `advanced.write` / `presets.save` | Validate first; create durable one-shot workflow (with baseline/recipe snapshots) **before** the first machine write; own `active_workflow_id` during the write; on confirmed success: terminal + idempotency commit then prompt-release auto-owned BLE (if durable terminal rolls back: raise, keep pending + ownership, never claim success/release); on confirmed rollback / clear pre-write failure (connect, preflight, workflow create): IDEM_FAILED + prompt-release any auto-owned link (including pre-existing orphan; never explicit debug); on partial/unconfirmed: keep pending, retain workflow + BLE, no auto-release/retry; recovery stop/cancel: `status=recovery_released` / `ownership_released_unconfirmed`, `machine_cancel=false`, original write stays pending forever |
| `settings.read` / `advanced.read` | Connect if needed; no durable workflow; return complete result; prompt-release one-shot/workflow auto-owned links after success or failure; never release explicit debug |
| Confirmed natural terminal or confirmed cancel/stop | Commit durable terminal state/event (and idempotency when applicable) **first**, then `close_session` + `disconnect` |
| Persistence failure after confirmed machine terminal | `recovery_required`; **do not** claim release; keep connection |
| `stop_unconfirmed` / `control_unconfirmed` | Keep recovery state; **do not** auto-release; pending request_id never reissues |
| Unexpected BLE disconnect (no activity/workflow) | Detach client ownership; `connected=false`, `connection_scope=null`; preserve address for a later explicit op; settle `disconnected`; **no** recovery invented; **no** auto-reconnect |
| Unexpected BLE disconnect (active durable workflow) | Detach stale client safely; preserve address; keep `activity` + `active_workflow_id`; persist `ble_disconnected` + recovery; surface `recovery_required`; loaded coffee needs fresh armed reconcile; loaded tea fail-closed; running/paused/starting/unconfirmed stay those phases with recovery (never rewritten back to a false confident running); **no** auto-reconnect, load, or start |
| Explicit `recovery.reconcile` (matching `workflow_id`) | Under `_op_lock`: at most one connect attempt + status query using the fresh state-generation gate; **never** load/start/control writes. **Loaded coffee** requires fresh `armed` to clear recovery (never re-load). **Loaded tea** has no positive protocol marker and stays `recovery_required` (idle/ready/complete are *not* tea-loaded proof and do **not** terminalize a never-started tea workflow). Fresh terminal (`ready`/`complete`/`idle`) terminalizes **only** workflows that progressed beyond loaded (running/paused/starting/unconfirmed/etc.), then releases. Fresh armed (coffee) / active / paused proof → reattach monitoring, durable reconcile, clear recovery only after persist succeeds. Connect/query failure → remain recovery, retain durable ownership; keep link if established. No periodic reconnect |
| In-flight BLE drop during a client wait | Transport loss wakes ACK futures and notification-queue waits with a domain `XBloomError` (never `Future.cancel` / `CancelledError`). Bridge maps that to `BridgeError` + pending/recovery; genuine task cancellation still propagates as `CancelledError` |
| Connect failure while phone/external owns radio | Stable category `device_busy_external` (unavailable vs busy may be indistinguishable); one attempt only; no retry, preemption, or background reconnect; detail preserved in error/status |
| Load/preflight failure after **auto-connect** only | Disconnect the new link |
| Load/preflight failure on pre-existing **explicit** link | Keep the debug connection |
| Orphan leftover auto-owned link (no activity, no active/recovery workflow, scope `workflow`/`one-shot`) | `XBLOOM_BRIDGE_IDLE_DISCONNECT_S` safety-net only (default 300s; `0` disables). Prompt terminal release remains immediate; timer never auto-reconnects |
| Disconnect failure after a confirmed terminal | Keep `last_operation` and durable terminal; surface `last_disconnect_error`; no machine-action retry |
| After release | Daemon stays `running=true`; **do not** auto-reconnect or preempt a phone/external client; next hardware RPC may reconnect once |

## RPC contract (protocol v3)

- Mutating RPCs that enforce v3 idempotency require `request_id`: load/start/pause/resume/stop/cancel, grinder/water/scale start/tare/live water adjust, **and** `settings.write` / `advanced.write` / `presets.save`. Read-only `settings.read` / `advanced.read` do not require `request_id`. `connect`/`disconnect` are not machine-action idempotent and are not claimed as such.
- `recovery.reconcile` requires matching active `workflow_id`; it is **not** a machine-mutating write (no load/start/control) and does not use the idempotency table. It may connect once and query only.
- Start/pause/resume/normal stop/cancel require matching active `workflow_id` **before** any BLE write for **new** requests. Exact completed duplicates return the SQLite-cached result **before** phase/cooldown/activity gates (no second BLE write). Pending `request_id` → `recovery_required` (never retry). Method/params conflicts raise without a second write.
- **Grinder SQLite guard (complete):** sole authority is durable workflows + terminal events in `state.db` (no runtime coffee/tea/grinder `*-state.json`). `status.grinder_guard` states: `ready` | `cooldown` | `recovery_required` | `unavailable` (fail closed). Durable nonterminal grinder workflow is created **before** the motor write. Confirmed STOP terminalizes with `grinder_stopped_at` / `grinder_cooldown_until` / `grinder_rest_seconds` (60s). Unconfirmed STOP retains recovery + BLE (no prompt release). Daemon restart does **not** auto-BLE; explicit cancel reconnects once for STOP only (no `grinder_start`). Exact completed `grinder.start` duplicates return cache **before** cooldown/activity/BLE gates. Migrated multi-active nonterminal recovery aborts the whole import (backup + originals intact; DB rolled back).
- Emergency stop/cancel: `emergency=true` may act on the active workflow despite missing/stale ID; response and durable terminal event mark `emergency`. Duplicates after terminal still cache safely.
- After a machine load/write may have happened, failed ACK keeps pending idempotency (no auto-release, no reissue). Confirmed rollback is terminal failed and retryable.
- Confirmed terminal + matching request completion share one SQLite transaction; natural terminals use the same commit without `request_id`.
- `status` / `events` require no `request_id` and never initiate BLE.
- `events` with `workflow_id` + `since` returns durable machine/phase/terminal rows, `next_since`, and explicit `gap_detected` / `gap_reason`.

## Status observability

- `connection_scope`: `explicit` | `workflow` | `one-shot` | `null` when disconnected
- `release_pending`: scheduled prompt release waiting on `_op_lock`
- `last_disconnect_reason` / `last_disconnect_time` / `last_disconnect_error` (`ble_disconnected` for unexpected drops)
- `idle_disconnect_s`, `idle_orphan_since`, `idle_orphan_deadline` (orphan fallback only; read-only — status does not arm/reset)
- `active_workflow_id`, `workflow` (durable summary), `recovery`, instance/core/protocol versions

## Race safety

Terminal machine events may arrive while a control RPC holds `_op_lock`. Release is scheduled on the event loop and only disconnects after acquiring `_op_lock`, so it does not deadlock, does not disconnect under an in-flight write, and does not hide the terminal `last_operation`.

Bridge-initiated close/disconnect unbinds the client disconnect listener and marks the disconnect expected **before** `close_session`/`disconnect`, so expected prompt or explicit release never invents recovery. Stale unexpected callbacks after terminal (generation unbound) are ignored. Connect-time drops are not bound as owned until the link is up (listeners attach after successful connect) so half-open connects do not invent recovery. A drop while a mutating RPC is in flight wakes the wait with a domain error, leaves that `request_id` pending/unconfirmed (never retryable), and returns a normal `BridgeError` RPC path — not task cancellation. Persistence failure while recording drop/reconcile fails closed in memory (`recovery_required` retained). `recovery.reconcile` runs under `_op_lock`; `status`/`events` remain read-only and do not connect.

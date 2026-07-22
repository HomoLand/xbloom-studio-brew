# BLE connection lifecycle (implemented slice)

Status: implemented in `packages/core/xbloom_ble/bridge.py` + `xbloom_storage.StateStore` with fake-client tests.

**Implemented Phase A core slice:** A1 (durable workflow on load), A2 (workflow_id + request_id + idempotency + emergency stop), A4 (transactional terminal before release), A8 (status/events durable fields), focused A10 tests.

**Still not full Phase A:** A3 client/HTTP disconnect semantics beyond core, A5 settings one-shot auto-release, A6 full machine reconciliation on restart, A7 idle disconnect timer, A9 Skill/Web/MCP client cutover, A11 hardware validation.

## Rules in force

| Situation | BLE behavior |
|---|---|
| Daemon / `BridgeCore` construction, `status`, `events` | No connect |
| Explicit `connect` RPC | Connect; `connection_scope=explicit`; hold until explicit `disconnect` |
| Coffee / tea `load` | Create durable workflow + snapshot **before** BLE load write; connect if needed (`workflow`); return `workflow_id`; reuse connection through start/pause/resume/events/terminal |
| Coffee / tea still `loaded` (awaiting start) | Hold the workflow connection; wait for start or explicit cancel; **no** time-based cancel, unload, expiry, or disconnect |
| Grinder / water / scale start | Durable one-shot `workflow_id`; connect if needed (`one-shot`); reuse until that op ends |
| Confirmed natural terminal or confirmed cancel/stop | Commit durable terminal state/event (and idempotency when applicable) **first**, then `close_session` + `disconnect` |
| Persistence failure after confirmed machine terminal | `recovery_required`; **do not** claim release; keep connection |
| `stop_unconfirmed` / `control_unconfirmed` | Keep recovery state; **do not** auto-release; pending request_id never reissues |
| Load/preflight failure after **auto-connect** only | Disconnect the new link |
| Load/preflight failure on pre-existing **explicit** link | Keep the debug connection |
| Disconnect failure after a confirmed terminal | Keep `last_operation` and durable terminal; surface `last_disconnect_error`; no machine-action retry |
| After release | Daemon stays `running=true`; next hardware RPC may reconnect once |

## RPC contract (protocol v3)

- Mutating RPCs that enforce v3 idempotency require `request_id`: load/start/pause/resume/stop/cancel and grinder/water/scale start/tare/live water adjust. **Not yet**: `settings.write` / `advanced.write` / `presets.save` (A2/A5 follow-up). `connect`/`disconnect` are not machine-action idempotent.
- Start/pause/resume/normal stop/cancel require matching active `workflow_id` **before** any BLE write for **new** requests. Exact completed duplicates return the SQLite-cached result **before** phase/cooldown/activity gates (no second BLE write). Pending `request_id` → `recovery_required` (never retry).
- Emergency stop/cancel: `emergency=true` may act on the active workflow despite missing/stale ID; response and durable terminal event mark `emergency`. Duplicates after terminal still cache safely.
- After a machine load write may have happened, failed ACK keeps `load_unconfirmed` / pending idempotency (no auto-release, no reissue).
- Confirmed terminal + matching request completion share one SQLite transaction; natural terminals use the same commit without `request_id`.
- `status` / `events` require no `request_id` and never initiate BLE.
- `events` with `workflow_id` + `since` returns durable machine/phase/terminal rows, `next_since`, and explicit `gap_detected` / `gap_reason`.

## Status observability

- `connection_scope`: `explicit` | `workflow` | `one-shot` | `null` when disconnected
- `release_pending`: scheduled prompt release waiting on `_op_lock`
- `last_disconnect_reason` / `last_disconnect_time` / `last_disconnect_error`
- `active_workflow_id`, `workflow` (durable summary), `recovery`, instance/core/protocol versions

## Race safety

Terminal machine events may arrive while a control RPC holds `_op_lock`. Release is scheduled on the event loop and only disconnects after acquiring `_op_lock`, so it does not deadlock, does not disconnect under an in-flight write, and does not hide the terminal `last_operation`.

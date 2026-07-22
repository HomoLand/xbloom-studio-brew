# BLE connection lifecycle (implemented slice)

Status: implemented in `packages/core/xbloom_ble/bridge.py` with fake-client tests.
This is **not** full Phase A (no workflow IDs, request idempotency, SQLite cutover, idle timer, or hardware validation).

## Rules in force

| Situation | BLE behavior |
|---|---|
| Daemon / `BridgeCore` construction, `status`, `events` | No connect |
| Explicit `connect` RPC | Connect; `connection_scope=explicit`; hold until explicit `disconnect` |
| Coffee / tea `load` | Connect if needed (`workflow`); reuse for start/pause/resume/status/events through the workflow |
| Coffee / tea still `loaded` (awaiting start) | Hold the workflow connection; wait for start or explicit cancel; **no** time-based cancel, unload, expiry, or disconnect |
| Grinder / water / scale start | Connect if needed (`one-shot`); reuse until that op ends |
| Confirmed natural terminal or confirmed cancel/stop | Finish state cleanup first, then `close_session` + `disconnect` |
| `stop_unconfirmed` / `control_unconfirmed` | Keep recovery state; **do not** auto-release |
| Load/preflight failure after **auto-connect** only | Disconnect the new link |
| Load/preflight failure on pre-existing **explicit** link | Keep the debug connection |
| Disconnect failure after a confirmed terminal | Keep `last_operation`; surface `last_disconnect_error`; no machine-action retry |
| After release | Daemon stays `running=true`; next hardware RPC may reconnect once |

## Status observability

- `connection_scope`: `explicit` | `workflow` | `one-shot` | `null` when disconnected
- `release_pending`: scheduled prompt release waiting on `_op_lock`
- `last_disconnect_reason` / `last_disconnect_time` / `last_disconnect_error`

## Race safety

Terminal machine events may arrive while a control RPC holds `_op_lock`. Release is scheduled on the event loop and only disconnects after acquiring `_op_lock`, so it does not deadlock, does not disconnect under an in-flight write, and does not hide the terminal `last_operation`.

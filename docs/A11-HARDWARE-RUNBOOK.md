# A11 + H00 hardware runbook

Supervised validation after code merge. Target firmware: `V12.0D.500`.
Full backlog: `skills/xbloom-studio-brew/references/hardware-validation.md`.

## Before you start

1. Person at the machine; fully close/disconnect the phone xBloom App.
2. Confirm idle machine, water source, and physical stop within reach.
3. One BLE owner only — prefer:

```text
cd skills/xbloom-studio-brew
python scripts/bootstrap.py --dev   # if needed
python scripts/xbloom.py bridge start
python scripts/xbloom.py bridge status
```

4. Record date, firmware, commands, states, meter/cup readings, cleanup.

## A11 — Workflow BLE lifecycle (closes Phase A)

Goal: one coffee workflow, **one** BLE connect, confirmed terminal release, phone can reconnect, next client can start a new workflow.

| Step | Action | Pass criteria |
|------|--------|----------------|
| 1 | `bridge status` → disconnected, running | `connected=false`, daemon up |
| 2 | `load` a small supervised recipe | `status=armed`, non-empty `workflow_id` |
| 3 | Note connect count / `connection_scope` | `connection_scope=workflow`, single connect |
| 4 | `start --workflow-id … --confirm-ready cup-filter-water-beans` | start accepted; no accidental pause |
| 5 | `pause` then `resume` | phases match; same connection |
| 6 | Let complete **or** `cancel` after observation | durable terminal; history written |
| 7 | `bridge status` | `connected=false`, `running=true`, BLE released |
| 8 | Open phone official App, connect | App connects successfully |
| 9 | Disconnect App; from Skill or Web start another load | reconnect works; no `device_busy` loop |
| 10 | (optional) With App connected, Skill load | `device_busy_external`, no preemption |

**Fail if:** second connect mid-brew; terminal rolled back to running; App cannot reconnect after release; start retried on unconfirmed control.

## H00 — Start transient regression

After A11 step 4 (or a dedicated short load/start):

- Telemetry may show transient `awaiting_confirm` → `starting`.
- Must **not** send command `40518` pause on that transient.
- Cancel after minimum observation if not brewing a full cup.

## Minimal command sketch (Skill)

```text
python scripts/xbloom.py validate assets/hot-template.yaml
python scripts/xbloom.py load <recipe.yaml>
# copy workflow_id from JSON
python scripts/xbloom.py start --workflow-id <id> --confirm-ready cup-filter-water-beans
python scripts/xbloom.py monitor --workflow-id <id> --duration 120
python scripts/xbloom.py bridge status
```

Owner gates (`XBLOOM_ENABLE_REMOTE_START`, etc.) must already be set on the **daemon** environment before `bridge start`.

## Evidence blurb (paste into notes)

```text
Test ID: A11
Date / firmware:
workflow_id:
Connect count observed:
Pause/resume: yes/no
Terminal state:
BLE released: yes/no
Phone App reconnect: yes/no
Next Skill/Web workflow: yes/no
Notes:
```

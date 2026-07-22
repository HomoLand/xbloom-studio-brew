# Hardware validation backlog

Updated 2026-07-14. This checklist records supervised xBloom Studio work that remains after
deterministic protocol tests. The current hardware target is firmware `V12.0D.500`.

Use **D/T/H/P** with the definitions in `apk-capability-matrix.md`. A BLE write or optional report
is not physical-effect evidence. Never publish the machine address, serial, account data, or raw
telemetry capture.

## Before every session

- Keep a person at the machine and fully close/disconnect the phone xBloom App.
- Confirm firmware, idle state, selected water source, and the command-specific physical setup.
- Let the loopback bridge be the sole BLE owner; top-level active commands use typed bridge RPC,
  and only passive `scan` / `doctor --scan` discover BLE directly.
- Keep the physical stop available. Preserve an uncertain state and inspect it before retrying.
- Record date, firmware, command, requested values, reports/ACKs, visible result, measurements, and
  cleanup state. Do not upgrade evidence beyond what was actually observed.

## Already hardware-observed

- Discovery/session handshake, redacted machine info, coffee load/start/pause/resume/cancel, coffee
  bypass, A/B/C atomic save and AUTO transition.
- Scale enter/read/re-tare/exit, including mandatory entry auto-zero and signed readings.
- FreeSolo base dispense, running `center -> spiral`, explicit STOP echo `4507`, quit `8013`, and
  return to idle. The observed 200 ml RT run was intentionally stopped near 100 ml.

These need only a release-regression run when their code path changes.

## P0: close the important physical gaps

### H00 — Coffee start transient-state regression

Incident evidence from 2026-07-14 on firmware `V12.0D.500`: load reached `armed`; start then
reported transient `awaiting_confirm`, the Omni moved, and an immediate command `40518` paused it.
The CLI returned `start is unconfirmed`. This is not a cup-movement or flash-recipe failure.

- [ ] With the patched client, load a small supervised coffee recipe and start it once.
- [ ] Confirm telemetry goes `awaiting_confirm -> starting` without command `40518` when commit
  auto-proceeds.
- [ ] Cancel after the minimum useful observation unless a normal cup is intentionally planned;
  record emitted commands, states, and cleanup.

Pass when a transient `awaiting_confirm` never causes `40518`, while the simulated persistent-
awaiting branch remains covered by a fresh status recheck before its single fallback write.

### H01 — FreeSolo natural completion and volume telemetry

- [ ] Put an empty heat-safe vessel of at least 250 ml on the platform before starting.
- [ ] Dispense 120 ml at `RT`, current/`auto` source, 3.5 ml/s, center pattern; do not stop it.
- [ ] Confirm natural report `40511`, terminal `complete`, and clean FreeSolo exit.
- [ ] Record target, peak `40523` `dispensed_water_ml`, final `cup_delta_g`, and their differences.

Pass when natural completion is distinguished from explicit STOP and the peak machine meter is
within the guarded tolerance of 120 ml. Cup delta is observational and need not equal the meter.

### H02 — Omni Tea Brewer end to end

- [ ] Load a one-stage tea recipe and cancel before execution; confirm no water was dispensed.
- [ ] Supervise a one-stage 90 ml fill with approximately 120 ml displayed output.
- [ ] Record initial fill, soak report, `40520`/`bypass_started` finish, terminal state,
  `dispensed_water_ml`, and `cup_delta_g` without assuming which quantity includes siphon water.
- [ ] Repeat with an account-style `90 + 80 ml` two-stage recipe and confirm derived
  `grandWater = 42.5` for 4 g leaf plus two complete siphon cycles.

Do not add a synthetic 30 ml pour and do not send invented tea pause/resume controls.

### H03 — Running FreeSolo temperature A/B

- [ ] Use a thermometer and a 300 ml heat-safe vessel.
- [ ] Start a 250-300 ml bounded dispense at 50-60 C, then request 80 C while running.
- [ ] Measure outlet temperature at fixed intervals and record command time, water meter, first
  measurable rise, stabilized value, and lag. An optional `8108` report is not thermometer proof.
- [ ] Let the session finish naturally if safe; otherwise use physical/explicit STOP and record it.

Pass when the write remains non-blocking without a required optional ACK and a measured physical
temperature response is attributable to command `4510`.

### H04 — Standalone grinder bridge state machine

- [ ] Prepare a small bean dose and receiving cup; run a bounded 10-second bridge session.
- [ ] Observe start, pause, resume, confirmed STOP, quit, and idle cleanup.
- [ ] Immediately request another run and confirm the persisted 60-second rest lock refuses it
  before another motor start.

Record size, RPM, active motor time, each state/report, and cleanup outcome.

## P1: completeness checks

### H05 — Water-source routing

- [ ] Only when both are physically available, run a small RT dispense from `tank` and from
  `tap`/direct feed; verify the selected source supplies the water.
- [ ] Verify `auto` follows the decoded current machine setting without changing it.
- [ ] Keep persistent source-setting validation separate from per-dispense routing.

### H06 — Persistent user settings

- [ ] With the machine idle, capture the complete baseline.
- [ ] Change one field at a time: weight unit, temperature unit, brightness, then water source.
- [ ] Confirm exact readback and visible machine/App behavior, then restore the original field before
  testing the next one.

Do not switch persistent water source unless the destination supply is safe and available.

### H07 — Mechanical tuning

- [ ] Capture radius and vibration-amplitude baselines.
- [ ] Move each by one adjacent APK level, confirm readback and a supervised visible effect, then
  restore the baseline before continuing.
- [ ] Record any unconfirmed rollback as a blocker and inspect the official App before another write.

### H08 — Paused live adjustment and remaining patterns

- [ ] After H03, test temperature and pattern writes while FreeSolo is paused, then resume.
- [ ] Test `center <-> circular` and `circular <-> spiral` transitions separately.
- [ ] Record physical motion/temperature independently from optional `8107`/`8108` reports.

Do not generalize the existing running `center -> spiral` evidence to these paths.

## Optional release regression

- [ ] Run one complete conservative hot coffee and compare recipe target, machine water meter, and
  cup-scale delta through natural terminal cleanup.
- [ ] Run one flash serving only when measured ice and the receiving vessel are ready; Studio runs
  the normal coffee pour-over program, while ice remains physical preparation outside BLE/App data.

## Deliberately excluded

- Firmware flashing/YMODEM, grinder zero calibration, raw scale calibration, and undocumented
  service operations.
- Fault injection merely to suppress optional live-setter reports; scripted BLE tests cover missing
  report behavior without creating an unsafe physical uncertainty.
- Arbitrary in-cycle coffee recipe editing or tea intervention commands that the APK does not expose.

## Evidence record template

```text
Test ID:
Date / firmware:
Recipe or command fingerprint:
Physical setup and measuring instrument:
Requested values:
Required ACK/report observations:
Optional report observations:
Visible or measured physical result:
Machine meter / cup delta:
Terminal and cleanup state:
Evidence level earned:
Notes / follow-up:
```

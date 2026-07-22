# Omni Tea Brewer recipes

Tea uses xBloom Studio's dedicated tea recipe mode and the Omni Tea Brewer siphon accessory. It is
not a coffee recipe with `grind: 0`. The dedicated protocol uploads cup geometry and a tea recipe
blob, then keeps execution as a separate hot-water command.

## Requirements and physical setup

- Omni Tea Brewer accessory installed exactly as xBloom documents.
- Studio firmware `V12.0D.300` or newer; this project's tested firmware is `V12.0D.500`.
- Usually 3-5 g tea leaves; the guarded schema enforces that range.
- A heat-safe receiving vessel centered on the scale. Do not move the tea brewer or vessel during
  the recipe because scale changes participate in siphon detection.
- Leave enough time between repeated steeps for the siphon cycle to finish.

The official manual method adds about 90 ml for steeping and then about 40 ml to trigger the siphon.
Automatic mode represents the same physical cycle differently: each recipe stage programs the
tea-chamber fill (commonly 80 or 90 ml), while the app presents approximately 120 ml of finished
output per selected steep. On the user's Studio, a `~120 ml` selection visibly dispensed about
90 ml, soaked, then released about 30 ml during the finish. That split is useful hardware evidence,
but it is not a universal exact `90 + 30` recipe formula: official stages can use 80 ml, retained
water varies, and the UI value itself is approximate.

Keep three volume layers distinct:

1. `pours[].ml` is the encoded chamber-fill target for each programmed steep.
2. `output_ml_per_steep` is app-display metadata (`~120`, `~240`, or `~360` total for one, two, or
   three selected steeps); it is not uploaded as a stage.
3. Machine `dispensed_water_ml` and scale `cup_delta_g` are runtime observations and can differ from
   both because the accessory and leaves retain water.

The firmware reports command `40520` using a generic `bypass` work-mode name during the post-soak
finish/siphon phase. This is **not** coffee's user-configurable post-brew bypass. Tea editor forms
explicitly disable bypass while retaining unused `bypassVolume`/`bypassTemp` placeholders inherited
from a shared form model. Do not add a 30 ml stage or enable coffee bypass to reproduce the finish;
the firmware owns it after the programmed fill and soak.

## Bundled official templates

The assets reproduce xBloom's public templates as retrieved on 2026-07-12:

| Asset | Leaf | Programmed stages | Temperature | Pause | Reported output |
| --- | ---: | --- | --- | --- | ---: |
| `tea-green-official.yaml` | 4 g | 90 + 90 ml | 85 + 85 C | 20 + 15 s | ~120 ml/steep |
| `tea-white-official.yaml` | 4 g | 90 + 90 ml | 99 + 99 C | 30 + 30 s | ~120 ml/steep |
| `tea-flower-official.yaml` | 4 g | 90 + 90 ml | 90 + 90 C | 30 + 20 s | ~120 ml/steep |
| `tea-black-official.yaml` | 4 g | 90 + 90 ml | 99 + 99 C | 30 + 25 s | ~120 ml/steep |
| `tea-oolong-official.yaml` | 4 g | 90 + 90 ml | 99 + 99 C | 15 + 10 s | ~120 ml/steep |

These are official starting points, not universal truths for every tea. Leaf age, shape, roast,
compression, and expansion affect extraction and when the siphon triggers. After tasting, adjust
one of leaf mass, temperature, or pause at a time. If siphoning triggers early/late, adjust water
conservatively according to the official troubleshooting guide rather than compensating with many
simultaneous changes.

These bundled files pin the public share-page snapshot retrieved on 2026-07-12. Current account
records may legitimately differ: the China catalog checked on 2026-07-14 included official stages
whose second fill was 80 ml. When the user asks for the current app recipe, prefer the freshly
authorized account record and preserve its provenance; do not silently rewrite either source to
make the snapshots match.

## Guarded schema

```yaml
name: My Tea
kind: tea
leaf_g: 4
output_ml_per_steep: 120
pours:
  - {label: Steep 1, ml: 90, temp_c: 85, pattern: circular, pause_s: 20, flow_ml_s: 3.5}
  - {label: Steep 2, ml: 90, temp_c: 85, pattern: center, pause_s: 15, flow_ml_s: 3.5}
```

Limits: 3-5 g leaves, 1-4 stages, 40-100 ml programmed per stage, 70-99 C, 1-120 s pause,
3.0-3.5 ml/s, and `center`/`spiral`/`circular` (`ring` remains a legacy alias). Unknown keys and raw protocol overrides are rejected.

The protocol suffix includes `grandWater`, which the app derives as total programmed chamber-fill
water divided by leaf mass. For example, `90 + 80 ml` at 4 g is `42.5`; it is not the approximate
finished-output ratio and does not encode a hidden 30 ml finish. The Skill derives it automatically.

## Validate, load, and start

Copy a bundled asset to a workspace path before editing, then:

```text
python scripts/xbloom.py tea-validate tea.yaml
python scripts/xbloom.py tea-load tea.yaml
```

`tea-load` uploads the cup and recipe data, verifies command acknowledgements, records the file
hash, and stops without executing it. To leave that pre-start state, use `cancel`.

Only after the user confirms that the tea brewer, leaves, water, receiving vessel, and surrounding
area are ready in the current interaction may an owner-enabled deployment execute:

```text
XBLOOM_ENABLE_REMOTE_START=I_UNDERSTAND_REMOTE_HOT_WATER
python scripts/xbloom.py tea-start tea.yaml \
  --confirm-ready tea-brewer-water-cup-clear
```

The loaded file must be unchanged and bound to the durable `workflow_id` on the same machine.
Loaded tea waits indefinitely for explicit start or cancel (no five-minute loaded expiry). Never
schedule unattended tea starts.

For a single held connection, `tea-brew` performs the same guarded load and explicit execute steps:

```text
XBLOOM_ENABLE_REMOTE_START=I_UNDERSTAND_REMOTE_HOT_WATER
python scripts/xbloom.py tea-brew tea.yaml \
  --confirm-ready tea-brewer-water-cup-clear
```

If the persistent bridge is already running, keep it as the sole BLE owner:

```text
python scripts/xbloom.py bridge tea-load tea.yaml
python scripts/xbloom.py bridge tea-start \
  --confirm-ready tea-brewer-water-cup-clear
python scripts/xbloom.py bridge events --since 0
```

The bridge preserves the same file hash / workflow identity, owner opt-in, and per-start readiness
checks (no time-based loaded expiry). `bridge cancel` exits a loaded or active tea workflow; tea
phase reports are telemetry and do not create unsupported pause/resume controls.

It emits separate `tea_loaded` and `start_accepted` events, then monitors to a terminal machine
state. Telemetry may include `tea_phase` (`soaking`, `paused`, or `running`), a
`last_report` of `bypass_started` for firmware report `40520`, changed soak seconds,
the recipe's `target_dispensed_water_ml`, cumulative machine `dispensed_water_ml`, and cup-scale
`cup_delta_g`. The latter two are observations of the current operation, not water-supply inventory.
Do not equate programmed water with reported siphon output: water retained in leaves/accessory and
the tea cycle's mechanics make them intentionally different quantities.

## Protocol verification boundary

The Java tea-code builder, derived `grandWater`, native pause encoding, and the distinct `40520`
finish phase were independently ported from the official Android app. Golden tests pin the official
green-tea blob, an account-style `90 + 80 ml` ratio/suffix, minute pause encoding, command frames,
load/execute separation, fake-BLE acknowledgements, and named soak/pause/restart reports. The
dedicated tea execution command has not been fired by unattended release tests; first physical
runs must remain supervised at the machine. The APK exposes tea-phase reports, but this Skill does
not invent unproven manual tea pause/resume commands from them.

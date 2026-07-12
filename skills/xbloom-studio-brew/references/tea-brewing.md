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
In xBloom's five public automatic templates, each stage contains a programmed 90 ml value while the
public UI reports roughly 120 ml output per steep. Keep those two values distinct; do not change the
machine field to 120 merely to match the display output.

## Bundled official templates

The assets reproduce xBloom's public templates as retrieved on 2026-07-12:

| Asset | Leaf | Programmed stages | Temperature | Pause | Reported output |
| --- | ---: | --- | --- | --- | ---: |
| `tea-green-official.yaml` | 4 g | 90 + 90 ml | 85 + 85 C | 20 + 15 s | 120 ml/steep |
| `tea-white-official.yaml` | 4 g | 90 + 90 ml | 99 + 99 C | 30 + 30 s | 120 ml/steep |
| `tea-flower-official.yaml` | 4 g | 90 + 90 ml | 90 + 90 C | 30 + 20 s | 120 ml/steep |
| `tea-black-official.yaml` | 4 g | 90 + 90 ml | 99 + 99 C | 30 + 25 s | 120 ml/steep |
| `tea-oolong-official.yaml` | 4 g | 90 + 90 ml | 99 + 99 C | 15 + 10 s | 120 ml/steep |

These are official starting points, not universal truths for every tea. Leaf age, shape, roast,
compression, and expansion affect extraction and when the siphon triggers. After tasting, adjust
one of leaf mass, temperature, or pause at a time. If siphoning triggers early/late, adjust water
conservatively according to the official troubleshooting guide rather than compensating with many
simultaneous changes.

## Guarded schema

```yaml
name: My Tea
kind: tea
leaf_g: 4
output_ml_per_steep: 120
pours:
  - {label: Steep 1, ml: 90, temp_c: 85, pattern: ring, pause_s: 20, flow_ml_s: 3.5}
  - {label: Steep 2, ml: 90, temp_c: 85, pattern: center, pause_s: 15, flow_ml_s: 3.5}
```

Limits: 3-5 g leaves, 1-4 stages, 40-100 ml programmed per stage, 70-99 C, 1-120 s pause,
3.0-3.5 ml/s, and `center`/`spiral`/`ring`. Unknown keys and raw protocol overrides are rejected.

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

The loaded file must be unchanged, on the same machine, and less than five minutes old. Never
schedule unattended tea starts.

## Protocol verification boundary

The Java tea-code builder and native pause encoding were independently ported from the official
Android app. Golden tests pin the official green-tea blob, minute pause encoding, command frames,
load/execute separation, and fake-BLE acknowledgements. The dedicated tea execution command has not
been fired by unattended release tests; first physical runs must remain supervised at the machine.

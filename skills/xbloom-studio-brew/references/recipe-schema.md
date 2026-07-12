# Guarded recipe schema

Create recipes as local UTF-8 YAML or JSON files. Use the bundled templates in `assets/` and
validate every edited recipe before any BLE write.

This page defines coffee and flash-brew files. Omni Tea Brewer files use a deliberately separate
schema and protocol; read `tea-brewing.md` and validate them with `tea-validate`.

## Top-level fields

| Field | Required | Guarded meaning |
| --- | --- | --- |
| `name` | Recommended | Human-readable recipe name. |
| `kind` | Yes | Canonical value `hot` or `flash-brew`. |
| `dripper` | Recommended | Must contain `Omni` when supplied. |
| `dose_g` | Yes | 5-18 g. |
| `grind` | Yes | 35-75; larger is coarser. Use `0` for pre-ground/grinder-off. |
| `ratio` | Yes | Sum of extraction pours divided by dose; bypass is excluded. For flash brew this is the extraction ratio. |
| `water_ml` | Yes | Pours plus bypass for `hot`; pours plus bypass plus ice for `flash-brew`. |
| `hot_water_ml` | Recommended/Yes | Sum of extraction pours; required for `flash-brew`. |
| `bypass_ml` | Optional | Machine-executed post-brew bypass, whole 5-100 ml. Omit/zero to disable. |
| `bypass_temp_c` | With bypass | `RT`, guarded numeric 80-95 C, or `BP`; forbidden without `bypass_ml`. |
| `ice_g` | Flash only | 40-180 g; forbidden on hot recipes except zero/omitted. |
| `time` | Optional | Display-only expected range; quote it as a string. |
| `note` | Optional | Display-only preparation or flavor note. |
| `stage_temps` | Yes | Keep exactly `[110.0, 90.0]`; these are decoded staging fields, not pour temperatures. |
| `pours` | Yes | Two to five ordered pour mappings. |

For a hot recipe without bypass, sum of pours, `hot_water_ml`, and `water_ml` match. With bypass,
`hot_water_ml` remains the extraction-pour total while `water_ml = hot_water_ml + bypass_ml`.
The extraction ratio may be 1:8 through 1:20 when bypass is enabled, and the final water ratio must
remain 1:12 through 1:20. Total machine water must be 60-360 ml.

For a flash brew, sum of pours must equal `hot_water_ml`; `water_ml` must equal
`hot_water_ml + bypass_ml + ice_g`. Extraction ratio must be 1:8 through 1:14 and final ratio must
be 1:12 through 1:20.

## Pour fields

| Field | Required | Guarded meaning |
| --- | --- | --- |
| `label` | Optional | Human-readable stage name. |
| `ml` | Yes | 10-127 ml. Automatic protocol splitting is disabled. |
| `temp_c` | Yes | `RT`, 80-95 C, or `BP`. |
| `pattern` | Yes | `spiral`, `ring`, or `center`. First pour cannot be `center`. |
| `agitation` | Yes | Boolean; allowed only for a spiral first pour and at most once. |
| `pause_s` | Yes | 0-60 seconds after the pour. |
| `rpm` | Yes | 60-120 in 10-RPM steps for non-center pours; `0` for center. Repeat one non-zero value. |
| `flow_ml_s` | Yes | 3.0-3.5 ml/s in 0.1 increments. |

The reverse-engineered encoder carries RPM in the first pour section and zeroes it in later wire
sections. Treat it as recipe-level grinder RPM and repeat it in non-center YAML pours to make the
intent explicit. Do not use a center first pour because that would make this field ambiguous.

`RT` and `BP` are the app's J15 mode tokens (`20` and `98` on the decoded model). `RT` means
room-temperature/pass-through and does not actively cool water to exactly 20 C. The base protocol
model can represent numeric 40-95 C, but the Agent-facing guarded schema deliberately keeps
ordinary coffee stages at 80-95 C.

## Example

```yaml
name: Balanced Light Roast
kind: hot
dripper: Omni Dripper 2
dose_g: 15
grind: 56
ratio: 16
water_ml: 240
hot_water_ml: 240
time: "2:25-3:00"
note: First-cup baseline; change one variable after tasting.
stage_temps: [110.0, 90.0]
pours:
  - label: Bloom
    ml: 45
    temp_c: 93
    pattern: spiral
    agitation: true
    pause_s: 35
    rpm: 100
    flow_ml_s: 3.0
  - label: Main
    ml: 105
    temp_c: 93
    pattern: spiral
    agitation: false
    pause_s: 10
    rpm: 100
    flow_ml_s: 3.3
  - label: Finish
    ml: 90
    temp_c: 92
    pattern: ring
    agitation: false
    pause_s: 0
    rpm: 100
    flow_ml_s: 3.3
```

## Rejected input

The wrapper rejects unknown keys, remote recipe URLs, YAML object constructors, raw frames,
protocol opcodes, sequence/tail overrides, out-of-envelope values, inconsistent totals, and
load frames containing brew-control opcodes.

Do not call the vendored `xbloom_ble` modules directly from an Agent workflow. Always use
`scripts/xbloom.py`, which applies this schema and the operational safety gates.

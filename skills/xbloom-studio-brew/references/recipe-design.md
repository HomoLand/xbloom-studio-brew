# Recipe design guide

Use this guide to turn bean information into one conservative first-cup recipe. Treat every
recipe as a starting hypothesis, not a claim of an objectively best extraction.

## Contents

- Inputs and defaults
- Choose a hot profile
- Adjust for process, age, and water
- Build a flash brew
- Convert C40 clicks
- Diagnose by changing one variable
- Report the result

## Inputs and defaults

Prefer these inputs, in descending order of usefulness:

1. Drink style: hot, flash-brew/iced, clarity, sweetness, body, or low bitterness.
2. Roast level and roast date.
3. Process: washed, honey, natural, anaerobic, or another fermentation-heavy process.
4. Origin, variety, altitude, and producer.
5. Roaster tasting notes.
6. Water type and any previous recipe or drawdown result.

If information is missing, state the assumptions. Use 15 g coffee and 240 g final water as
the general default. Assume filtered, pleasant-tasting water; never silently assume RO water.
Use the Omni Dripper 2 unless the user explicitly says otherwise.

Larger xBloom grind numbers are coarser. Use two xBloom steps as the normal grind correction.
Keep all values concrete in the generated YAML; put adjustment ranges only in the prose.

## Choose a hot profile

Select the row that best matches the user's flavor target, then adjust for the bean. These are
deliberately narrower than the machine's full range and fit the guarded controller.

| Target and coffee | Grind | Display temp | Grinder RPM | Ratio | Pours |
| --- | ---: | ---: | ---: | ---: | ---: |
| Light, dense, washed; floral or tea-like clarity | 48-54 | 93-95 C | 100-110 | 1:16-16.5 | 3-4 |
| Light-medium; balanced fruit and sweetness | 54-58 | 92-94 C | 90-100 | 1:15.5-16 | 3 |
| Medium; caramel, nut, chocolate, daily balance | 58-62 | 90-92 C | 80-90 | 1:15-16 | 3 |
| Medium-dark or dark; low bitterness | 62-68 | 86-90 C | 70-90 | 1:14.5-15.5 | 3 |
| Expressive Ethiopian-style natural; floral berry clarity | 50-54 | 92-93 C | 100 | 1:16 | 3-4 |

Start with one of these pour structures:

- Balanced 240 ml: 45 / 105 / 90 ml.
- Layered 240 ml: 45 / 65 / 65 / 65 ml.
- Lower-agitation 225 ml: 40 / 100 / 85 ml.

Use a spiral bloom, spiral main pour, and spiral or ring finish. Enable agitation only on the
first pour. Keep adjacent pour temperatures within 0-2 C unless a tasted result justifies more.
Use 3.0 ml/s for the bloom and 3.2-3.5 ml/s afterward. Prefer fewer pours for slow-draining,
fine-heavy, dark, or strongly fermented coffees.

The YAML schema repeats `rpm` on each non-center pour for validation, but it represents one
recipe-level grinder speed in the currently decoded protocol. Repeat one value throughout.

## Adjust for process, age, and water

Apply only the smallest relevant adjustment to the chosen roast profile.

### Process

- Washed and high-density: keep the base temperature; use the finer half of the grind range
  when the target is clarity and the drawdown is not slow.
- Honey: begin with the base row; favor three pours and bloom-only agitation.
- Natural: begin 1-2 steps coarser or 1 C cooler than a comparable washed coffee when the cup
  risks dryness or fermented heaviness. Do not suppress a clean, floral natural automatically.
- Anaerobic or fermentation-heavy: use three pours, bloom-only agitation, and the lower half
  of the profile's temperature range.
- Dark roast: avoid combining fine grind, high temperature, long pauses, and agitation.

### Roast age

- Very fresh coffee with abundant gas: extend the first pause by 5-10 seconds without changing
  another variable.
- Rested coffee in its normal window: use the baseline.
- Older coffee that tastes flat: first try 1-2 steps finer. Raise temperature by 1 C only if
  drawdown and bitterness do not suggest over-extraction.

### Water

- Moderately mineralized filtered water: use the baseline.
- RO or very low-mineral water: do not compensate with a large temperature jump. After tasting,
  try 1-2 steps finer or 3-5 seconds more bloom pause, one change at a time.
- Hard or alkalinity-heavy water that tastes dull: recipe changes have limited reach; note that
  water treatment may matter more than further machine tuning.

## Build a flash brew

Use `kind: flash-brew` for Japanese-style coffee brewed hot over ice. This is the closest
automated xBloom option to an iced Americano, but it is not espresso and must be described as
"Americano-style flash brew" or "冰美式风格闪冲" rather than a true Americano.

Design from final beverage water first:

1. Choose a final ratio of about 1:15-16 for the first cup.
2. Put 35-40% of final water into clean ice in the receiving vessel.
3. Brew the remaining 60-65% as hot water through the coffee.
4. Use a grind 2-4 steps finer and a temperature 1-2 C hotter than the comparable hot recipe,
   while staying inside the guarded limits.
5. Use three pours and bloom-only agitation.

Reliable 15 g starting point:

- Final water: 240 g.
- Hot brew water: 150 g, so the machine ratio is 1:10.
- Ice: 90 g, so the final ratio is 1:16.
- Pours: 40 / 60 / 50 ml.
- Grind: 52 for a light or light-medium coffee; move coarser for darker or bitter-prone coffee.
- Temperature: 94 / 93 / 92 C.
- RPM: 100.

In flash-brew YAML, `ratio` is the hot machine-water ratio and must equal the sum of pours
divided by dose. `water_ml` is the final water and must equal `hot_water_ml + ice_g`.

Do not ask the machine to dispense cold bypass water. Put the measured ice in the receiving
vessel before loading the recipe. Use a vessel large enough for the final drink and melting ice.

## Convert C40 clicks

Use this compatibility table only when the user gives a Comandante C40 setting. It is adapted
from the source recipe skill's published conversion data; grinder calibration and burr version
can shift the result, so always treat it as an initial mapping.

| C40 clicks | Approximate xBloom setting |
| ---: | ---: |
| 11 | 1 |
| 15 | 10 |
| 18 | 20 |
| 22 | 30 |
| 26 | 41 |
| 30 | 50 |
| 33 | 60 |
| 36 | 70 |
| 40 | 80 |

Interpolate linearly between the nearest anchors and round to a whole xBloom step. Then apply
the roast/process correction once. The guarded BLE controller accepts only settings 35-75, so
stop and explain if the converted setting falls outside that operational envelope.

## Diagnose by changing one variable

Use taste plus flow behavior. Never change grind, temperature, agitation, and ratio together.

| Result | First correction | If still present |
| --- | --- | --- |
| Sharp, hollow, sour; fast drawdown | Grind 2 steps finer | Add 3-5 s to bloom pause |
| Sour but slow or flooded | Raise temperature 1 C | Reduce early water or check filter/setup |
| Bitter, dry, astringent | Grind 2 steps coarser | Remove agitation, then lower 1 C |
| Muted but otherwise clean | Grind 1-2 steps finer | Raise 1 C |
| Heavy fermented note | Lower 1-2 C | Grind 2 steps coarser |
| Thin flash brew | Grind 2 steps finer | Raise hot-water temperature 1 C |
| Harsh flash brew | Grind 2 steps coarser | Lower hot-water temperature 1 C |
| `WAIT`, flooding, or very slow drawdown | Check physical setup, then grind 2-4 steps coarser | Use fewer pours and no agitation |

For `WAIT`, first confirm the scale tape is removed, the cup is not touching the machine, the
cup was not moved, the correct dripper is selected, and the filter is seated and draining.

## Report the result

Return both:

1. A short human-readable rationale and a one-variable next-cup correction.
2. A complete local YAML file conforming to `recipe-schema.md`.

Name the flavor goal, state any assumptions, show hot and final ratios separately for flash
brews, and never claim the recipe has been taste-validated before the user brews it.

# Community statistical baselines (advanced)

Optional **advanced** starting points derived from community analysis of ~450
app-visible xBloom recipes (mostly official / high-engagement). Adapted for this
Skill from [Saievo/xbloom-CoT-Brew](https://github.com/Saievo/xbloom-CoT-Brew)
knowledge notes (analysis date 2026-05-04). **Not** an official xBloom dataset.

## How to use with the conservative guide

1. Always start from `recipe-design.md` **conservative** profiles for a first cup
   unless the user already has a catalog recipe for this bean.
2. Prefer **user catalog / history** over any template here (exact lot beats
   population statistics).
3. Use this file for **advanced** first-cup hypotheses, comparison after
   research, or when the user asks for “official-style / data-driven” baselines.
4. Every output must still pass `validate` / core schema. Statistics are not
   permission to exceed guarded ranges.
5. Pattern **names** in YAML are `center` / `circular` / `spiral`. Do not copy
   numeric pattern IDs from other projects.

## Confidence

| Claim family | Confidence | Notes |
| --- | --- | --- |
| Roast → grind / temp / ratio medians | High | Large n; trends clear |
| Process → bloom water/pause | Medium-high | Adequate after grouping |
| Pattern sequences | Medium | High individual variance |
| RPM choice | Medium | 120 rpm dominates; other speeds weakly patterned |
| Clog risk rules | Medium | Small supervised sample on cake filters |

## Population maps (summary)

### Roast → grind (xBloom steps; larger = coarser)

Community medians cluster around **55–60**. Light roasts are often **finer**
than medium-light (density), not coarser. This Skill’s conservative light
clarity band (48–54) is **finer still** on purpose.

### Roast → temperature

Typical full-brew averages ~**90–93 C**, falling slightly as roast darkens
(~2 C light→dark). Bloom often ~0.5–1 C warmer than later pours in community
data; our conservative guide prefers flat or mild step-down.

### Roast → ratio

Mostly **1:15–17**; light slightly higher (1:16–17), dark slightly lower
(~1:15).

### RPM

**120** is the modal official-app choice. This Skill often uses **80–110** for
clarity/control. Prefer 100–120 for dense light washed; 60–80 only with a
stated fine-reduction goal (e.g. some Geisha).

### Process → bloom (× dose)

| Process group | Bloom water | Bloom pause | Notes |
| --- | --- | --- | --- |
| Washed | ~3.3× | ~25 s | Baseline bloom |
| Natural | ~3.5× | ~23–25 s | Slightly more bloom water |
| Honey | ~3.7× | ~26 s | Highest bloom water |
| Special ferment / anaerobic | ~3.4× | ~26 s | Longer rest; watch aroma burn-off |
| Decaf | ~2.8× | ~22 s | Lower bloom water |

### Pour count

Community recipes often use **4–5** pours. This Skill still defaults to **3**
pours for first cups (simpler, fewer mid-bed shocks). Use 4–5 only as advanced
or when matching a known good catalog recipe.

## Advanced bean templates (15 g dose)

Scale water with ratio. Patterns are YAML names. Vibration: prefer bloom
`after` only unless clog risk is low and the user wants more agitation.

### A — Light washed Africa (Ethiopia/Kenya/Rwanda)

- Ratio ~1:16.5, grind ~54–57, rpm 100–120, bloom 3.3× / 25–30 s, ~93 C then
  mild step-down, spiral→circular finish, 4–5 pours optional.

### B — Light natural Africa

- Ratio ~1:16–16.5, grind ~56–58, bloom ~3.5×, temps may stay high (~93–95) if
  the cup stays clean; watch fermented heaviness.

### C — Anaerobic / co-ferment

- Ratio ~1:16, grind ~56–58, longer bloom pause, moderate temp; optional low
  bloom (82–88 C) then rise to ~91–93 C for aroma protection (advanced).
  Prefer three pours and bloom-only vibration.

### D — Medium-dark natural Americas

- Ratio ~1:15–16, grind ~55–60, clear step-down temperature curve, circular
  heavy, shorter pauses.

### E — Medium washed Americas

- Ratio ~1:16, grind ~58–60, spiral bloom optional, alternate spiral/circular.

### F — Decaf

- Lower bloom water (~2.8×), 3–4 pours, stable ~91–92 C.

### G — Honey

- Higher bloom water (~3.7×), grind mid-fine, can use more pours for sweetness
  layering; **raise clog risk** (see below).

## Vibration (community)

- **After** vibration is far more common than before.
- U-shape: bloom after high; middle pours low; last large pour sometimes after.
- Circular stages co-occur with after-vibration more than center.

### Clog risk (Omni cake filter) — important

Cake filters can seal if fines + vibration pack the bed. Risk factors:

| Factor | Weight |
| --- | ---: |
| Honey / natural (soft structure, fines) | +2 |
| Grind ≤ 57 | +1 |
| ≥ 5 pour stages | +1 |
| Nordic extreme light / brittle | +1 |
| RPM 60 alone | +0.5 |

If score **≥ 3**, disable bloom `after` vibration and prefer fewer pours /
coarser grind before chasing extraction with finer grinds.

Misread: channel-induced mixed sour+bitter+thin often looks like “under”
extraction; **coarsen to restore flow** rather than grind finer.

## Geisha / Gesha — do not treat as one density class

| Style | Tendency |
| --- | --- |
| Panama washed high-grown | Fine + hot (e.g. grind 50–57, 93–95 C, often lower RPM) |
| Other washed Geisha | Slightly coarser/cooler than Panama peaks |
| Ethiopia natural Geisha | **Much lower** extraction ceiling (often 88–91 C, grind 62–68) |

When **Geisha + natural** both apply, process often outweighs variety density
myths.

## Temperature ramp-up (minority strategy)

~12% of community recipes rise mid-brew. Small +1–3 C after cooler bloom is
common mild aroma care; large +5–13 C (cool bloom then hot body) is mostly for
anaerobic/special ferment. Do **not** use ramps on dark roasts by default.

## Flash brew alignment

Community “ice reconstruction” agrees with `recipe-design.md` flash rules:

- Hot machine water ~**1:10** for a final ~1:16 with vessel ice.
- Finer and slightly hotter than the hot twin; longer bloom (~45 s) is common
  for flash; three pours.
- Ice mass is **local metadata** only (`ice_g`); machine never dispenses ice.

Prefer the explicit flash section in `recipe-design.md` for YAML fields.

## Dial-in quick map

| Cup fault | First move |
| --- | --- |
| Bitter / dry | Coarser +2–3, or −1–2 C, less spiral/vibration |
| Sour / sharp | Finer +2–3, or +1–2 C, more contact |
| Thin / watery | Lower ratio slightly or finer; more pours only if flow allows |
| Heavy / muddy | Higher ratio or coarser; reduce agitation |
| Clog / WAIT | Physical checks first, then coarsen, fewer pours, no mid vibration |

Change **one** variable per cup after tasting.

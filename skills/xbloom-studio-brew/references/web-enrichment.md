# Web enrichment and public recipe comparison

Use web research to improve evidence, not to replace brewing judgment. Keep the offline Skill
baseline available whenever web tools are absent or reliable sources cannot be found.

## Trigger research

Search when at least one condition holds:

- The user names an exact roaster, coffee, producer, or lot.
- Critical bean metadata is missing but the product identity is searchable.
- The user asks for recipes from baristas, cafes, roasters, champions, or the xBloom community.
- A direct xBloom recipe or recipe-card equivalent may exist.
- The coffee is an xPod, or the user has its NFC Recipe Card/package.

Skip research when the bean is generic, the user asks for a quick/offline answer, or no web tool is
available. State that the result uses the bundled offline model.

## Rank sources

Prefer sources in this order:

1. Exact coffee plus a native xBloom recipe published by its roaster, cafe, named author, or xBloom.
2. Exact coffee plus a manual brew guide on the roaster/author's official site.
3. A native xBloom recipe for a closely matching roast, process, density, and flavor target.
4. A general method published by a named coffee professional, cafe, or roaster.
5. Community posts only when the user requests them; label them anecdotal and never present them as
   professional or official recipes.

Prefer the original publisher over reposts, search summaries, aggregators, and retailer copies.
Do not call someone a champion or expert unless a first-party or competition source supports it.
Never bypass logins, paywalls, app authentication, or private recipe cards.

## Classify the source device

Every published reference must carry one of these classes before adaptation:

- **xPod-native:** recipe intended for xBloom's pre-portioned xPod brewer/pod geometry.
- **Omni-native:** recipe intended for loose beans in an Omni Dripper.
- **Tea-native:** recipe intended for the Omni Tea Brewer siphon accessory.
- **Manual flat-bottom:** manual recipe whose bed geometry is directionally similar to Omni.
- **Manual cone:** method inspiration only; do not copy its pours mechanically.

Do not collapse xPod-native and Omni-native into a generic "xBloom recipe". The same machine can
execute both, but pod/dripper geometry and coffee preparation differ.

## Verify and extract

Verify coffee identity: roaster, producer, origin, variety, process, roast description/date, and
publication date. Reject a same-name recipe when the lot or process does not match.

Capture only available facts:

- Author/publisher and direct URL.
- Original brewer and filter.
- Dose, total water, ratio, water temperature, grind reference, pour structure, agitation, time,
  water chemistry, and stated flavor goal.
- Whether it is native xBloom, flat-bottom manual, cone manual, or a general technique.

Do not invent missing parameters. Quote sparingly; summarize the method and cite the source.

## Adapt to xBloom

- **Native xBloom:** preserve the source intent, then apply the guarded schema and explain any
  values changed for safety or current firmware.
- **xPod-native:** treat the original roaster recipe as high-value first-party evidence for
  temperature, ratio, pour intent, flavor target, and dial-in direction, but do not run it unchanged
  on Omni. Rebuild it conservatively for the Omni bed and label the result an adaptation.
- **Flat-bottom manual:** treat as a medium-confidence adaptation. Map pulse structure, ratio, and
  temperature; derive xBloom grind/RPM conservatively.
- **Cone brewer:** treat as flavor/method inspiration only. Rebuild pours for the Omni's flat bed
  instead of copying center pours or drawdown expectations.
- **Different dose:** rescale water mathematically, then recalculate every pour; do not exceed 18 g.
- **Missing machine values:** use the Skill baseline and label each inferred value.

Every adapted file must pass `scripts/xbloom.py validate`. A public recipe never overrides firmware,
hot-water, state, or opcode safety gates.

## xPod and NFC Recipe Cards

xPod is valuable reference data when it is the exact coffee: the bag/card represents xBloom and the
roaster's intended recipe rather than a generic model guess. xBloom states that current bags use one
NFC Recipe Card per bag (older individual xPods carried NFC). Use the printed/app-visible recipe and
flavor information when the user can provide it.

Do not claim the NFC tag itself contains the full recipe. Public material confirms that tapping the
card selects the paired recipe, but does not establish whether the NDEF payload stores all parameters
or only an identifier resolved elsewhere. A future direct-import workflow must first capture a
user-owned card's NDEF records and compare them with the app-visible recipe. Until then, treat NFC as
a recipe-selection source, not a decoded executable format.

## Present choices

When credible sources exist, show no more than three options:

1. **Skill baseline** — offline bean model, most conservative first cup.
2. **Closest published reference** — cite author/publisher and explain the xBloom adaptation.
3. **Alternative flavor direction** — include only when a second credible source or materially
   different target exists.

For each option show source, original brewer, bean-match quality, intended flavor, key parameters,
adaptation confidence, and the important differences. Ask the user to choose before writing/loading
the final recipe unless they explicitly delegate the choice. Keep source citations in the response
or a companion note, never as extra keys in executable recipe YAML.

Useful discovery seeds include xBloom's roaster-curated recipe ecosystem, Standout Coffee's public
xBloom Studio case study, and roaster-owned brew-guide libraries such as April Coffee's. Treat these
as starting domains, not a permanent whitelist; verify the current page and author every time.

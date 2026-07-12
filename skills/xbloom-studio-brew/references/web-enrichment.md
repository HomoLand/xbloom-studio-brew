# Web enrichment and public recipe comparison

Use web research to improve evidence, not to replace brewing judgment. Keep the offline Skill
baseline available whenever web tools are absent or reliable sources cannot be found.

## Trigger research

Search when at least one condition holds:

- The user names an exact roaster, coffee, producer, or lot.
- Critical bean metadata is missing but the product identity is searchable.
- The user asks for recipes from baristas, cafes, roasters, champions, or the xBloom community.
- A direct xBloom recipe or recipe-card equivalent may exist.

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
- **Flat-bottom manual:** treat as a medium-confidence adaptation. Map pulse structure, ratio, and
  temperature; derive xBloom grind/RPM conservatively.
- **Cone brewer:** treat as flavor/method inspiration only. Rebuild pours for the Omni's flat bed
  instead of copying center pours or drawdown expectations.
- **Different dose:** rescale water mathematically, then recalculate every pour; do not exceed 18 g.
- **Missing machine values:** use the Skill baseline and label each inferred value.

Every adapted file must pass `scripts/xbloom.py validate`. A public recipe never overrides firmware,
hot-water, state, or opcode safety gates.

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

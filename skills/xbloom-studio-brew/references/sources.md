# Sources and evidence boundaries

Checked 2026-07-12. Prefer official xBloom material for physical specifications, the vendored
project for decoded BLE behavior, and tasting feedback for recipe correction.

## Official xBloom material

- [xBloom Studio product page](https://xbloom.com/products/xbloom-studio) — product capabilities.
- [Getting started](https://tbdxsupport.zendesk.com/hc/en-us/articles/25198204646939-Getting-started) — operating limits, Bluetooth, vessel, and hot-water guidance.
- [Brewing with the Omni Dripper 2](https://tbdxsupport.zendesk.com/hc/en-us/articles/25915864027675-How-do-I-brew-my-favorite-coffee-beans-with-the-Omni-Dripper-2) — dose and dripper workflow.
- [Settings](https://tbdxsupport.zendesk.com/hc/en-us/articles/25198572238875-Settings) — firmware display and machine controls.
- [Iced coffee brewing methods](https://xbloom.com/blogs/news/iced-coffee-brewing-methods) — official iced/flash-brew context.

Official specifications do not document the private BLE command protocol used here.

## Public recipe research examples

- [xBloom Studio overview](https://xbloom.com/pages/xbloom-studio) — xBloom describes app-accessible,
  roaster-curated recipes; availability does not mean the full parameters are public on the web.
- [Standout Coffee: Brewing Standout Coffee on the xBloom Studio](https://www.standoutcoffee.com/blogs/news/brewing-standout-coffee-on-the-xbloom-studio-by-erik-persson)
  — a roaster-published xBloom Studio case study crediting Erik Persson and documenting taste-led
  grind adjustment of a Standout double-bloom starting recipe.
- [April Coffee: Coffee info and recipes](https://www.aprilcoffeeroasters.com/pages/coffee-inf-recipes)
  — a roaster-owned library of coffee-specific and base manual recipes. These use April/EK43
  references and require explicit Omni/xBloom adaptation.

Use these as evidence examples, not as a fixed recommendation list. Re-open the current source,
verify the author and brewer, cite it, and label every adaptation.

## Community projects incorporated under MIT

- [ryunana/xbloom-studio-recipe-skill](https://github.com/ryunana/xbloom-studio-recipe-skill), commit
  `81dd5b334141492580b5b41cd23e2163184b161a` — source for recipe heuristics and C40 conversion
  starting points. This Skill narrows the device envelope, adds flash-brew semantics, does not
  default unknown water to RO, and treats all flavor guidance as a hypothesis requiring tasting.
- [Janczykkkko/xbloom-ble](https://github.com/Janczykkkko/xbloom-ble), commit
  `c8712a46821016affe752277e62db11e4c9039c0`, version 2.3.0 — source of the vendored BLE protocol,
  client, recipe model, telemetry parser, and upstream protocol tests. Upstream describes the
  project as alpha and tested only on firmware `V12.0D.500`.

See `THIRD_PARTY_NOTICES.md` and `licenses/` for attribution and license texts.

## Agent Skill compatibility

- [Agent Skills specification](https://agentskills.io/specification) — portable directory and
  `SKILL.md` format.
- [Hermes: Creating Skills](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills)
  — helper-script model, local path token, testing, publication, taps, and the Skill-versus-Tool
  boundary.

## Evidence labels

- **Official constraint:** stated in xBloom documentation.
- **Decoded behavior:** observed and implemented by the community BLE project; firmware-dependent.
- **Guarded policy:** a deliberately stricter limit imposed by this Skill.
- **Recipe heuristic:** a first-cup flavor hypothesis. Validate by taste and change one variable.
- **Published reference:** a cited public recipe or case study whose original brewer and adaptation
  must remain visible to the user.

This project is independent, unofficial, and not affiliated with or endorsed by xBloom.

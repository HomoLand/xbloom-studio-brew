# Sources and evidence boundaries

Checked through 2026-07-14. Prefer official xBloom material for physical specifications, the vendored
project for decoded BLE behavior, and tasting feedback for recipe correction.

## Official xBloom material

- [xBloom Studio product page](https://xbloom.com/products/xbloom-studio) — product capabilities,
  including hot or room-temperature volume-controlled water.
- [Getting started](https://tbdxsupport.zendesk.com/hc/en-us/articles/25198204646939-Getting-started) — operating limits, Bluetooth, vessel, and hot-water guidance.
- [Brewing with the Omni Dripper 2](https://tbdxsupport.zendesk.com/hc/en-us/articles/25915864027675-How-do-I-brew-my-favorite-coffee-beans-with-the-Omni-Dripper-2) — dose and dripper workflow.
- [Settings](https://tbdxsupport.zendesk.com/hc/en-us/articles/25198572238875-Settings) — firmware display and machine controls.
- [Iced coffee brewing methods](https://xbloom.com/blogs/news/iced-coffee-brewing-methods) — official iced/flash-brew context.
- [Three Creative Modes](https://tbdxsupport.zendesk.com/hc/en-us/articles/25198266531355-Three-Creative-Modes)
  — official FreeSolo scale/grinder/brewer ranges and usage.
- [Omni Tea Brewer how-to](https://xbloom.com/blogs/news/how-to-use-omni-tea-brewer) and
  [detailed support guide](https://tbdxsupport.zendesk.com/hc/en-us/articles/34937798170779-xBloom-Omni-Tea-Brewer-A-How-to-Guide)
  — accessory setup, leaf guidance, manual siphon sequence, firmware/app requirements, and links to
  xBloom's five public tea templates.
- [Tea and brewing temperature](https://xbloom.com/blogs/news/tea-and-brewing-temperature) — broad
  green/black/oolong/white/herbal starting ranges.
- [Tea siphon troubleshooting](https://tbdxsupport.zendesk.com/hc/en-us/articles/37704056850459-Why-does-the-tea-brewer-siphon-trigger-too-early)
  — leaf expansion, water adjustment, pause, scale, and movement guidance.
- [xPod and NFC packaging history](https://xbloom.com/blogs/news/specialty-coffee-sustainability) —
  xBloom's change from an NFC tag on every xPod to one Recipe Card per bag.

Official specifications do not document the private BLE command protocol used here.

## Official Android interoperability analysis

FreeSolo and tea commands were independently derived for interoperability from
[xBloom's public Android download](https://tbdprodproducts.s3.amazonaws.com/downloads/xbloom_coffee_release.apk),
`xbloom_coffee_release.apk` (retrieved 2026-07-12, SHA-256
`29624db558917e6a975cd58a3123c240950d200a3adc4efe8ffef222e1b14c6e`). The project does not
redistribute the APK or decompiled sources.

The port is pinned by tests for:

- Generic J15 frame layout and CRC.
- Scale enter/tare/exit, signed readings, and both weight-report identifiers.
- Grinder enter/start/stop/quit arguments and cleanup ordering.
- Brewer enter/start argument encoding, tank/tap source, `TemperatureConstant.RT = 20.0`, and
  completion cleanup.
- Coffee bypass volume/temperature/dose encoding and recipe-stage RT/BP tokens.
- Independent recipe pattern plus four-state vibration timing (`none`, `before`, `after`, `both`).
- Fixed-width Studio machine-info decoding with serial redaction at the CLI boundary.
- Persistent unit/display/source commands, combined settings report, and type-2 radius/amplitude
  read/write frames with APK UI-level transforms.
- Cumulative dispensed-water report `40523`, separate cup-scale readings, pour/tea/error reports,
  and six-character xPod XID reports.
- Tea cup setup, recipe upload, execute separation, official green-tea blob, and native minute-pause
  transformation.
- Official coffee/tea, combined Studio-created, Product/xPod, Shared, current-Easy, and default-Easy
  endpoint/form models; recursive JSON/MMKV normalization; and the app's chunked
  RSA/PKCS#1-v1.5 request envelope. The legacy account APIs use the public key embedded in
  `BaseTransfer`; the APK's second `RSAEncrypt` key belongs to another request stack and is not
  interchangeable.
- Tea form compatibility details: bypass is explicitly disabled even though the shared edit model
  retains unused volume/temperature placeholders; `grandWater` is programmed stage water divided
  by leaf mass; and report `40520` names the firmware-owned post-soak finish `bypass` even though it
  is not configurable coffee bypass.

These are decoded, firmware-dependent behaviors, not an official xBloom API. On 2026-07-14, an
ephemeral owner-authorized China-tenant login verified read compatibility for
`tHostRecipe.thtml` (9), `tuTeaRecipe.tuhtml` (6), `tuMyTeaRecipeCreated.tuhtml` (2),
`tuMyRecipeProduct.tuhtml` (6), and `tuMyRecipeShared.tuhtml` (0). The combined created endpoint
contained both Studio coffee and tea; the older coffee-only created endpoint did not. Tokens, raw
responses, member IDs, and credentials were not persisted. The add-only `tuRecipeAdd.tuhtml` form
is decoded and mock-tested. Two owner-approved additions (one hot and one flash-brew extraction)
were also created and read back on 2026-07-14; subsequent exact replays returned `already-present`
without writing. That bounded owner action is compatibility evidence, not a release-time smoke test:
automated and manual release validation must not create disposable account records.

Scale enter/read/exit has also been verified on `V12.0D.500`; a follow-up hardware observation
confirmed that the entry
command zeros a load already present. On 2026-07-13, a supervised RT-water run on the same firmware
visually verified a running `center → spiral` command and confirmed explicit STOP echo `4507`, quit
`8013`, and return to idle. The run was deliberately stopped around 100 ml of a 200 ml target, so it
does not verify natural completion or physical live-temperature response. The temperature frame and
completed write path are separately verified, but not with an outlet thermometer. Persistent
settings/mechanical writes are also not physically tested by this project. Unattended tests do not
run the grinder or dispense water.

See `apk-capability-matrix.md` for the complete Studio command inventory, feature classification,
known cup-geometry/settings evidence boundaries, and the distinction between J15 BLE, J20 cloud code,
NFC lookup, and mobile-only features.

## Public official tea templates

xBloom's detailed support guide links the five share pages used for bundled assets:

- [Green tea](https://share-h5-test-cn.xbloomcoffee.cn/?id=850qMtt0WPvyTOSvvizGlw%3D%3D)
- [White tea](https://share-h5-test-cn.xbloomcoffee.cn/?id=FELD1GKNl68YmE7BV50nuA%3D%3D)
- [Flower tea](https://share-h5-test-cn.xbloomcoffee.cn/?id=KDqLjy1OqB4HRuPFY9ywgQ%3D%3D)
- [Black tea](https://share-h5-test-cn.xbloomcoffee.cn/?id=ysyArYOVGRv3I7SulAsmXA%3D%3D)
- [Oolong tea](https://share-h5-test-cn.xbloomcoffee.cn/?id=ZjhJp8TorA3kS9dFjZWgWA%3D%3D)

The public response distinguishes the 90 ml programmed stage value from the displayed approximate
120 ml per-steep output. It is test/public infrastructure, so the bundled files pin that public
snapshot. Current account records can differ—the 2026-07-14 China catalog included 80 ml second
stages—so future Agents must identify which source the user chose rather than silently merging them.

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

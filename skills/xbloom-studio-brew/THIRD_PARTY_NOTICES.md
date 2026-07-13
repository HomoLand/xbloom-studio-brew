# Third-party notices

This project is distributed under the MIT License in `LICENSE` and incorporates/adapts the
following MIT-licensed projects.

We gratefully thank **Janczykkkko** for publishing the reverse-engineered xBloom Studio BLE work
and **ryunana** for publishing the xBloom Studio recipe-engineering Skill. This project would not
exist in its current form without their open-source contributions.

## xbloom-ble

- Project: <https://github.com/Janczykkkko/xbloom-ble>
- Vendored commit: `c8712a46821016affe752277e62db11e4c9039c0`
- Upstream version: 2.3.0
- Copyright: Copyright (c) 2026 Janczykkkko
- License: `licenses/xbloom-ble-MIT.txt`
- Vendored files: `scripts/xbloom_ble/` and the corresponding upstream tests under `tests/`.

The vendored core is wrapped by a stricter local-file validator, firmware/state preflight,
load-only default workflow, persistent armed-state hash, and explicit remote-start gates. Its
package documentation was adjusted to describe this wrapper boundary accurately.

## xbloom-studio-recipe-skill

- Project: <https://github.com/ryunana/xbloom-studio-recipe-skill>
- Adapted commit: `81dd5b334141492580b5b41cd23e2163184b161a`
- Copyright: Copyright (c) 2026 ryunana
- License: `licenses/xbloom-studio-recipe-skill-MIT.txt`
- Adapted material: recipe archetypes, parameter heuristics, one-variable dial-in guidance, and
  C40-to-xBloom conversion starting points in `references/recipe-design.md`.

The adapted guidance was reorganized, paraphrased, narrowed to the guarded controller, extended
for flash brew, and separated into official constraints, decoded behavior, policies, and tasting
heuristics.

## Trademarks and affiliation

xBloom and xBloom Studio are names and trademarks of their respective owner. This independent,
unofficial project is not affiliated with, endorsed by, or sponsored by xBloom. The private BLE
protocol is reverse-engineered community work and may change with firmware.

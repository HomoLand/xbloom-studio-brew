# xBloom Studio Brew

[简体中文](README.zh-CN.md)

[![validate-skill](https://github.com/HomoLand/xbloom-studio-brew/actions/workflows/test.yml/badge.svg)](https://github.com/HomoLand/xbloom-studio-brew/actions/workflows/test.yml)
[![GitHub Release](https://img.shields.io/github/v/release/HomoLand/xbloom-studio-brew)](https://github.com/HomoLand/xbloom-studio-brew/releases)

A portable Agent Skill for designing bean-specific xBloom Studio coffee/tea recipes and using the
machine's guarded local Bluetooth LE capabilities, including its scale, grinder, and brewer.

It combines an offline coffee-recipe model, optional cited web research, a private app-visible
recipe catalog, deterministic validation, and bundled BLE control. It works with Hermes and other
Agent Skills-compatible clients.

> [!WARNING]
> This is an unofficial community project. The BLE protocol is reverse-engineered and can control
> a motor and a machine that dispenses near-boiling water. Recipe loading and scale reading are
> separated from physical starts; grinder and hot-water actions require deployment and per-action
> safety gates.

## What it does

- Designs hot pour-over and Americano-style flash-brew recipes from bean and taste information.
- Optionally researches public recipes from roasters, cafes, and named coffee professionals.
- Treats exact xPod/Recipe Card recipes as first-party references while adapting their pod geometry
  explicitly for loose-bean Omni brews.
- Includes five bundled xBloom-published Omni Tea Brewer templates and a dedicated guarded tea
  protocol; account sync can also import the region's current tea list.
- Imports authorized xBloom App/API or decoded-MMKV JSON into a private normalized coffee/tea
  catalog; ephemeral account sync covers official, user-created, Product/xPod, and shared lists.
- Previews local coffee/tea recipes as exact App forms and can explicitly perform an idempotent,
  add-only account upload without storing credentials or session tokens.
- Deletes only member-created cloud recipes after an offline preview and an exact owner confirm
  phrase; official/shared/product recipes stay read-only.
- Keeps a local brew journal from load/start/cancel/complete telemetry and can import coarser App
  brew-history records for phone-only sessions used in dial-in.
- Shows a conservative Skill baseline plus cited adaptations for the user to compare.
- Validates dose, extraction/final ratios, bypass, water totals, grind, temperature modes, pattern,
  four-state vibration timing, flow, RPM, and BLE opcodes before writes.
- Scans, probes, loads, monitors, cancels, saves A/B/C presets, and supports gated remote start.
- Uses the FreeSolo scale with explicit entry auto-zero semantics, the standalone grinder, and
  volume-controlled tank/direct-feed water at RT or 40-98 C.
- Includes a loopback-only persistent BLE bridge that serializes coffee, tea, scale, grinder,
  water, presets, settings, and tuning operations plus bounded event telemetry.
- Separates recipe target water, cumulative machine output, and cup-scale net increase; it does not
  mislabel any of them as water-supply inventory.
- Reads redacted Studio machine info plus persistent settings/mechanical tuning, with separately
  gated readback/rollback writes for units, display, source, pour radius, and vibration amplitude.
- Runs recipe design, catalog import/query, and BLE workflows locally without an app account;
  optional account sync/add uses ephemeral credentials and never stores credentials or raw sessions.

> [!IMPORTANT]
> Flash brew is a serving method, not a separate xBloom Studio program. The machine runs the same
> coffee pour-over protocol and dispenses only the programmed water; `kind: flash-brew`, `ice_g`,
> and final-water accounting are local metadata that tell the user to preload measured ice in the
> receiving vessel before starting. They are never sent as an iced-mode or ice command.

## How recipes are produced

```text
bean information
  -> bundled recipe knowledge
  -> optional first-party web evidence
  -> Agent reasoning and user choice
  -> guarded YAML validator
  -> local BLE controller
```

Web enrichment is evidence-first. An exact xBloom recipe from its original roaster or author ranks
above a manual brew guide; flat-bottom recipes are adapted explicitly, while cone-brewer recipes are
treated only as inspiration. Sources, original brewers, confidence, and adaptations remain visible.
An xPod-native recipe is valuable roaster intent, but it is not silently treated as Omni-native.
If no reliable source or web tool exists, the Skill falls back to its bundled offline model.
Web enrichment therefore requires a web-search tool configured in the host Agent; it never requires
credentials inside this Skill and never blocks offline recipe generation or local BLE control.

See [web enrichment policy](skills/xbloom-studio-brew/references/web-enrichment.md).

Hermes quick setup (restart its gateway afterward if it is already running):

```text
hermes config set web.search_backend ddgs
```

The full verification query is in the [deployment guide](skills/xbloom-studio-brew/references/deployment.md).

## Private recipe catalog

The APK does not bundle a global recipe database: it fetches regional, account/device-visible
records and caches them. This project can therefore collect every recipe present in an authorized
JSON/cache export, or every official, created, Product/xPod, and Shared record returned to the
user's own account and region; it cannot truthfully promise every private or worldwide xBloom
recipe.

```text
python scripts/xbloom.py catalog status
python scripts/xbloom.py catalog import-json app-response.json
python scripts/xbloom.py catalog import-mmkv decoded-mmkv.json
python scripts/xbloom.py catalog list --kind coffee --executable
python scripts/xbloom.py catalog list --kind tea
python scripts/xbloom.py catalog export <id> recipe.yaml
python scripts/xbloom.py catalog login-sync --region china --language zh-cn
python scripts/xbloom.py catalog push recipe.yaml --region china
python scripts/xbloom.py catalog delete --region china --table-id <id>
python scripts/xbloom.py catalog history-sync --region china
python scripts/xbloom.py history status
python scripts/xbloom.py history list --limit 20
```

The default catalog lives outside the installed Skill under
`~/.xbloom-studio-brew/catalog/catalog.json`. Raw responses and credentials are not retained.
xPod and J20 records stay reference-only; validated Studio coffee and tea records export through
their respective guarded YAML schemas. Ephemeral login, all five default read categories, and two
explicitly owner-approved add-only writes were live-service verified against the China tenant on
2026-07-14, including cloud readback and no-write idempotent replay. Sessions and credentials remain
memory-only.
Set `XBLOOM_ACCOUNT_EMAIL` and `XBLOOM_ACCOUNT_PASSWORD` in the host environment; passwords are
never accepted as command arguments. `catalog push` is preview-only unless the user explicitly
adds both `--apply` and `--confirm-write own-account-cloud-recipe`. `catalog delete` is also
preview-only unless the user adds both `--apply` and
`--confirm-delete own-account-cloud-recipe-delete`, and it only accepts a currently created
`tableId`. The local brew journal defaults to `~/.xbloom-studio-brew/brew-history.jsonl`. Live
cloud write/delete endpoints are not used by release tests. See the
[catalog and A/B/C guide](skills/xbloom-studio-brew/references/catalog.md).

## Install

### Hermes

```text
hermes skills install HomoLand/xbloom-studio-brew/skills/xbloom-studio-brew
```

Release `v1.0.1` and later pass Hermes' community-source guard as `SAFE`; the runtime scripts remain
fully scanned, while development-only tests and local cache/runtime directories are declared in
the standard `.skillignore`. The original `v1.0.0` source is functional, but Hermes' installer
blocked it after mistaking test fixtures and ordinary environment configuration reads for secrets.

### Other Agent Skills clients

Install or copy `skills/xbloom-studio-brew/` into the client's skills directory. The folder name
must remain `xbloom-studio-brew`.

### Bootstrap the local BLE runtime

From the installed Skill directory:

```text
python scripts/bootstrap.py
python scripts/xbloom.py doctor --scan
python scripts/xbloom.py probe
```

Bluetooth commands must execute on a local computer near the machine. Cloud sandboxes cannot reach
a home BLE adapter. The bundled bridge stays on that host and is not exposed as a LAN service.
Bootstrap stores its virtual environment under `~/.xbloom-studio-brew/runtime` by default, outside
the installed Skill, so read-only Agent caches and upgrades do not destroy it.

## Use

Example prompts:

```text
Use xbloom-studio-brew to design a clear, fruit-forward hot recipe for this coffee bag.
Find credible public recipes for this coffee and let me choose before creating the xBloom recipe.
Import my authorized xBloom recipe JSON, list the Studio coffee and tea recipes, and export one.
Create an Americano-style flash brew, validate it, and load it onto my xBloom Studio without starting.
Use the official green-tea template, but load it without starting.
Preview this local recipe for my xBloom account; do not upload it yet.
Help me weigh this empty cup: enter the scale with an empty platform, tell me when it is ready,
then I will place the cup.
```

The executable recipe is a local YAML file. Public-source citations stay in the response or a
companion note rather than becoming unsupported recipe keys.

Basic local commands:

```text
python scripts/xbloom.py scale --duration 30
python scripts/xbloom.py settings
python scripts/xbloom.py advanced
python scripts/xbloom.py catalog status
python scripts/xbloom.py tea-validate assets/tea-green-official.yaml
python scripts/xbloom.py tea-load assets/tea-green-official.yaml
python scripts/xbloom.py bridge start
python scripts/xbloom.py bridge status
python scripts/xbloom.py bridge scale-start --duration 90
python scripts/xbloom.py bridge tea-load assets/tea-green-official.yaml
python scripts/xbloom.py bridge stop
```

Tea volume has two layers: each 80/90 ml stage is programmed chamber-fill water, while the App's
`~120 / ~240 / ~360 ml` selector is approximate finished siphon output. After soaking, Studio
enters a firmware-owned phase reported as `bypass`; this is not the configurable coffee bypass and
is not encoded as an extra user-controlled 30 ml pour.

Scale entry automatically zeros the load already present. Start with an empty platform for an
object's absolute weight, or pre-position an empty vessel when measuring net contents; `--tare`
sends an additional re-tare. FreeSolo room-temperature water uses `water --temp RT` and remains
behind the same physical water-action gates as heated water. `--water-source auto` follows the
machine's current tank/direct-feed setting (`tap` is the CLI compatibility name); an explicit
source is required if it cannot be read.

`grind`, `water`, coffee `start`, `tea-start`, and one-connection `tea-brew` are included but
disabled until the deployment
owner enables their documented safety gate. See [standalone tools](skills/xbloom-studio-brew/references/standalone-tools.md)
and [tea brewing](skills/xbloom-studio-brew/references/tea-brewing.md).

Prefer the persistent bridge for device work. It supports coffee, tea, scale, grinder, FreeSolo
water, presets, settings, and tuning; while it runs, direct BLE commands refuse to race its
connection. Live FreeSolo temperature and
pattern targets are also protocol-implemented behind a separate owner gate. A running
`center → spiral` pattern change is hardware-verified on firmware `V12.0D.500`; live temperature
command encoding and its completed BLE write are verified, while physical outlet response remains
unmeasured. They do not change mid-run volume/flow or edit coffee recipe steps.

A/B/C programming is an atomic three-recipe operation. Run `validate <recipe.yaml> --slot` on
each input first. AUTO slots store pours, grind, ratio, and scale behavior; the machine measures
dose when a preset runs. They cannot represent post-brew bypass or tea, so every slot-writing layer
rejects bypass recipes instead of silently dropping water. Slot programming temporarily selects
PRO mode, writes A/B/C, confirms the saved state, and returns to AUTO without starting a brew.
Optional `--scale on off on` configures the three on-brew scale flags in A/B/C order.

## Safety model

- `load` sends guarded recipe frames and stops at `armed`; it does not start brewing.
- `tea-load` uploads a dedicated tea recipe but never executes it; `scale` reports its auto-zero
  baseline and always exits its mode.
- Firmware/state preflight runs before recipe or preset writes.
- Remote start requires an owner opt-in, current physical-readiness confirmation, the same recipe
  hash and machine, and an armed state less than five minutes old.
- Brew telemetry is aggregated to one progress update per second. Workflow state is cleared only
  after a terminal machine notification; a monitor timeout preserves recovery state for reattach
  or cancel, using the already-recorded machine instead of scanning.
- The tested firmware allowlist currently contains `V12.0D.500`; other firmware requires an explicit
  owner-level compatibility override.
- Every external or generated recipe must pass the same validator.
- Grinder runs are limited to 30 seconds with a persisted 60-second rest lock; stop/quit cleanup is
  attempted even on ordinary interruption.
- The persistent bridge binds only to loopback, authenticates local requests with a random token,
  owns one BLE connection, and serializes writes. Starting it alone does not connect or actuate.
- Interactive grinder control fails closed to STOP/QUIT on missing ACKs. Interactive water has a
  host-side safety timeout and requires an in-tolerance peak meter report before claiming natural
  completion. Explicit STOP is confirmed separately from the firmware's natural-completion report.
- Persistent machine-setting writes have their own owner/per-call gates, require an idle supported
  firmware, verify exact readback, and attempt baseline rollback. They are command-tested but not
  physically written by this project.

Read the complete [device safety policy](skills/xbloom-studio-brew/references/device-safety.md).

## APK capability coverage

The audited Android app contains more than Studio BLE control: it also includes cloud/account UI,
NFC lookup, store content, high-risk maintenance, and xBloom Original (`J20`) paths. The
[command-by-command APK capability matrix](skills/xbloom-studio-brew/references/apk-capability-matrix.md)
separates what is available directly or through the bundled bridge, what remains hardware-unobserved,
what is deliberately excluded, and
what is not a Studio device feature.

## Project layout

```
packages/core/                          # xbloom-studio-core: shared library
  xbloom_paths.py, xbloom_safety.py,    # BLE protocol, recipe validation, catalog,
  xbloom_catalog.py, xbloom_history.py  # history, paths, knowledge validation
  xbloom_knowledge.py                   #   knowledge bundle manifest/hash checks
  xbloom_ble/                           #   (bridge.py is the BLE owner state machine)
  pyproject.toml                        #   console entry: xbloom-bridge
skills/xbloom-studio-brew/              # the Agent Skill (CLI client of core)
  scripts/xbloom.py                     #   CLI entry point
  scripts/bootstrap.py                  #   runtime venv setup (stdlib-only until install)
  assets/, references/, agents/, tests/
tools/build_release.py                  # GitHub Release artifacts (not PyPI)
```

`packages/core` is the shared foundation imported by both the Skill CLI and the
Web UI backend. The Skill stays at `skills/xbloom-studio-brew/` and depends on
core via its `requirements.txt` (`-e ../../packages/core` in development). The
Web UI lives in a sibling repo (`xbloom-studio-web`) and its backend depends on
core the same way. The BLE bridge daemon (`xbloom_ble.bridge`) is a
single-instance loopback process shared by all clients; installable as
`xbloom-bridge` from the core wheel.

## Development

```text
cd skills/xbloom-studio-brew
python scripts/bootstrap.py --dev
```

Repository checkouts install core in editable mode from `../../packages/core`.
Release Skill bundles instead vendor the exact core wheel under `vendor/wheels/`
and bootstrap installs that path with `pip install --no-deps --no-index <wheel>` after
checking `vendor/release.json` (`core_wheel` + `core_wheel_sha256`, fail-closed).

## Build / release artifacts

Artifacts are published via **GitHub Releases** (not PyPI). From the repository root:

```text
python tools/build_release.py
# or: python tools/build_release.py --out dist
```

Builds set a stable `SOURCE_DATE_EPOCH`, pin setuptools to an exact build requirement, and
write ZIPs with normalized timestamps/modes so consecutive builds produce matching SHA-256 for
the wheel and both ZIPs. A `release-manifest.json` records name/version/size/SHA-256 for each
publishable artifact and is verified before the build finishes.

**Deferred (Phase 0.1 incomplete):** non-core per-platform `--hash` lockfiles for bleak/PyYAML
transitive wheels remain a follow-up; only the core wheel is hash-locked in the Skill bundle today.

This writes to `dist/` (gitignored):

| Artifact | Contents |
| --- | --- |
| `xbloom_studio_core-<ver>-*.whl` | Installable core library + `xbloom-bridge` |
| `knowledge-<ver>/` + `.zip` | `SKILL.md` + `references/` + `assets/` with `manifest.json` (per-file SHA-256 + aggregate content hash) |
| `skill-xbloom-studio-brew-<ver>/` + `.zip` | Self-contained Skill including `vendor/wheels/` core wheel + `vendor/release.json` |
| `release-manifest.json` | Deterministic name/version/size/SHA-256 for every publishable wheel/ZIP |

Verify a built wheel:

```text
python -m venv .venv-wheel
.venv-wheel/Scripts/python -m pip install dist/xbloom_studio_core-*.whl   # Windows
# .venv-wheel/bin/python -m pip install dist/xbloom_studio_core-*.whl    # Unix
.venv-wheel/Scripts/xbloom-bridge --help
```

Verify an extracted Skill **ZIP** in a fresh directory outside the checkout (do not use the
`dist/skill-.../` tree as a substitute for the published archive):

```text
# Unix example; on Windows extract to %TEMP%\xbloom-skill-clean similarly
python -c "import zipfile; zipfile.ZipFile('dist/skill-xbloom-studio-brew-<ver>.zip').extractall('/tmp/xbloom-skill-clean')"
cd /tmp/xbloom-skill-clean
python scripts/bootstrap.py
python scripts/xbloom.py doctor
python scripts/xbloom.py validate assets/hot-template.yaml
```

Release tests use scripted BLE and never activate the grinder or dispense water. The scale
enter/read/exit path and a running FreeSolo pattern change have separate supervised hardware
evidence on firmware `V12.0D.500`; see the capability matrix for exact evidence levels and the
[hardware validation backlog](skills/xbloom-studio-brew/references/hardware-validation.md) for the
remaining supervised checklist.

## Acknowledgements

With sincere thanks to the two upstream projects that made this Skill possible:

- [ryunana/xbloom-studio-recipe-skill](https://github.com/ryunana/xbloom-studio-recipe-skill)
  provided the recipe-engineering foundation, bean archetypes, dial-in heuristics, and C40 mapping.
- [Janczykkkko/xbloom-ble](https://github.com/Janczykkkko/xbloom-ble) provided the reverse-engineered
  xBloom Studio BLE protocol, client, telemetry parser, and protocol tests.

Both are incorporated/adapted under their MIT licenses. Pinned commits, modifications, copyright
notices, and full license texts are recorded in
[Third-party notices](skills/xbloom-studio-brew/THIRD_PARTY_NOTICES.md).

## License

MIT. xBloom and xBloom Studio are trademarks of their respective owner. This project is not
affiliated with or endorsed by xBloom.

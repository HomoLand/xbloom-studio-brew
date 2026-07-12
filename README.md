# xBloom Studio Brew

[简体中文](README.zh-CN.md)

A portable Agent Skill for designing bean-specific xBloom Studio coffee/tea recipes and using the
machine's guarded local Bluetooth LE capabilities, including its scale, grinder, and brewer.

It combines an offline coffee-recipe model, optional cited web research, deterministic recipe
validation, and bundled BLE control. It works with Hermes and other Agent Skills-compatible clients.

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
- Includes five xBloom-published Omni Tea Brewer templates and a dedicated guarded tea protocol.
- Shows a conservative Skill baseline plus cited adaptations for the user to compare.
- Validates dose, ratio, water totals, grind, temperature, flow, RPM, and BLE opcodes before writes.
- Scans, probes, loads, monitors, cancels, saves A/B/C presets, and supports gated remote start.
- Uses FreeSolo electronic scale (read/tare), standalone grinder, and exact-temperature/volume water.
- Runs locally without xBloom cloud credentials or an app account.

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

## Install

### Hermes

```text
hermes skills install HomoLand/xbloom-studio-brew/skills/xbloom-studio-brew
```

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
a home BLE adapter without a separately secured local bridge.

## Use

Example prompts:

```text
Use xbloom-studio-brew to design a clear, fruit-forward hot recipe for this coffee bag.
Find credible public recipes for this coffee and let me choose before creating the xBloom recipe.
Create an Americano-style flash brew, validate it, and load it onto my xBloom Studio without starting.
Use the official green-tea template, but load it without starting.
Read 10 seconds from the Studio scale without taring it.
```

The executable recipe is a local YAML file. Public-source citations stay in the response or a
companion note rather than becoming unsupported recipe keys.

Basic local commands:

```text
python scripts/xbloom.py scale --duration 30
python scripts/xbloom.py tea-validate assets/tea-green-official.yaml
python scripts/xbloom.py tea-load assets/tea-green-official.yaml
```

`grind`, `water`, coffee `start`, and `tea-start` are included but disabled until the deployment
owner enables their documented safety gate. See [standalone tools](skills/xbloom-studio-brew/references/standalone-tools.md)
and [tea brewing](skills/xbloom-studio-brew/references/tea-brewing.md).

## Safety model

- `load` sends guarded recipe frames and stops at `armed`; it does not start brewing.
- `tea-load` uploads a dedicated tea recipe but never executes it; `scale` always exits its mode.
- Firmware/state preflight runs before recipe or preset writes.
- Remote start requires an owner opt-in, current physical-readiness confirmation, the same recipe
  hash and machine, and an armed state less than five minutes old.
- The tested firmware allowlist currently contains `V12.0D.500`; other firmware requires an explicit
  owner-level compatibility override.
- Every external or generated recipe must pass the same validator.
- Grinder runs are limited to 30 seconds with a persisted 60-second rest lock; stop/quit cleanup is
  attempted even on ordinary interruption.

Read the complete [device safety policy](skills/xbloom-studio-brew/references/device-safety.md).

## Development

```text
cd skills/xbloom-studio-brew
python scripts/bootstrap.py --dev
```

The current suite contains 138 passing tests and 4 hardware/platform skips. Release tests never
activate the grinder or dispense hot water; the scale enter/read/exit path is hardware-verified on
firmware `V12.0D.500`.

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

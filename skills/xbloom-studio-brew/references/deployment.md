# Deployment and publication

This directory is a standard Agent Skill with a deterministic local CLI. The recipe workflow is
portable across Agents; BLE execution must run on the physical computer that has the Bluetooth
adapter and is near the xBloom Studio.

## Contents

- Runtime prerequisites
- Bootstrap
- Agent installation
- Environment configuration
- Publication layout
- Release checklist
- Architecture boundary

## Runtime prerequisites

- Python 3.11 recommended.
- Windows, macOS, or Linux supported by Bleak.
- Working Bluetooth Low Energy adapter and OS permission to scan/connect.
- Linux: a working BlueZ/D-Bus setup and user permission for Bluetooth access.
- Local execution. A remote cloud sandbox cannot reach a home BLE adapter; select a local Hermes
  terminal backend. The bundled persistent bridge is loopback-only and must run on that BLE host;
  it is not a remote-network gateway.

No xBloom account, cloud token, Android app credential, or internet connection is required for
recipe design, authorized JSON/cache import, validation, or BLE. Optional own-account catalog sync
and add-only local-recipe upload use ephemeral credentials as described in `catalog.md`; neither is
a dependency of machine control.

Web recipe enrichment is optional and uses the host Agent's own web-search tool. Configure a web
backend in Hermes or the target Agent when this feature is wanted; keep using the bundled offline
recipe model when none is available. Do not store search-provider credentials inside the Skill.

For Hermes, DDGS is a credential-free search backend:

```text
hermes config set web.search_backend ddgs
```

Restart a running Hermes gateway after changing its configuration. Validate the complete Agent
tool path (not merely the Python import) with a short forced-search query:

```text
hermes chat -Q -t web -s xbloom-studio-brew --max-turns 4 \
  -q "Use web_search to find the official xBloom Omni Tea Brewer page; return its title and URL."
```

Some Hermes installations lazily install the `ddgs` package on first use; managed deployments may
preinstall it into Hermes' own virtual environment. The package belongs to the host Agent, not this
Skill's BLE runtime.

## Bootstrap

From the skill directory, create its isolated per-user runtime:

```text
python scripts/bootstrap.py
```

For contributors, also install test dependencies and run the test suite:

```text
python scripts/bootstrap.py --dev
```

The bootstrap creates `~/.xbloom-studio-brew/runtime` (or the configured external runtime),
installs pinned dependencies, runs the doctor check, and, with `--dev`, runs tests. It never needs
to write into the installed Skill, which supports read-only caches and atomic Agent upgrades. The
main CLI automatically re-executes inside this runtime, so Agents can consistently call
`python scripts/xbloom.py ...`. A pre-migration Skill-local `.venv` remains a temporary fallback;
run bootstrap once to migrate, then remove that legacy directory only after `doctor` reports
`"runtime_location": "external"`.

Bootstrap uses only the Python standard library until dependencies are installed, so it can run
before `xbloom-studio-core` is present. It then chooses one of two layouts:

| Layout | How detected | Core install |
| --- | --- | --- |
| Development (repo checkout) | `requirements.txt` contains `-e ../../packages/core` and no release evidence | editable install of sibling `packages/core` |
| Release (GitHub Skill bundle) | `vendor/release.json` present, **or** a vendored `vendor/wheels/xbloom_studio_core-*.whl` | install the exact hashed wheel from `vendor/release.json` / `vendor/wheels/` with `pip install --no-deps --no-index <wheel>`, then pinned non-core deps |

Do not rely on a sibling monorepo checkout for release installs. The published Skill bundle carries
the exact core wheel it needs under `vendor/wheels/`. Bootstrap reads `vendor/release.json` for
`core_version`, `core_wheel`, and `core_wheel_sha256`. When that file is present it is parsed
strictly and fail-closed: malformed JSON, wrong types, `layout` other than `release`, version
mismatch, unsafe `core_wheel` (must be basename-only matching
`xbloom_studio_core-<version>-*.whl`, resolve as a direct child of `vendor/wheels/`, no `..` /
absolute paths / slash ambiguity), or a bad hash all abort before `pip` runs. There is no soft
fallback when metadata exists but is invalid.

**Damaged-bundle fail-closed:** if a vendored `xbloom_studio_core-*.whl` remains but
`vendor/release.json` is missing (or unreadable), layout detection still classifies the Skill as
**release** so bootstrap never falls through to development editable install or PyPI core install.
Install then **requires** valid `vendor/release.json` and aborts **before any `pip` invocation**
when that metadata is missing — it will not install an unhashed fallback wheel.

**Air-gapped hosts:** only **core** is installed offline from the bundle. `bleak` and `PyYAML` still
need network (or a pre-populated pip cache) unless you mirror those wheels yourself. Universal
`--hash` lockfiles for non-core deps are deferred because transitive wheels differ by platform
(winrt-*, dbus-fast, etc.). **Phase 0.1 is not fully complete** until per-platform non-core hash
lockfiles are generated in CI; core integrity is already bound by the vendored wheel plus
`core_wheel_sha256`.

## Build and release artifacts

Release artifacts are built for **GitHub Releases**, not PyPI. From the repository root (not the
Skill directory):

```text
python tools/build_release.py
python tools/build_release.py --out dist
```

`tools/build_release.py` aims for byte-for-byte reproducible wheel and ZIP digests by fixing
`SOURCE_DATE_EPOCH` (default `1704067200` unless already set), pinning the setuptools build
requirement to an exact version, and writing ZIPs with explicit `ZipInfo` (normalized timestamp,
Unix file mode, sorted members, `ZIP_DEFLATED` at a fixed compression level). A
`release-manifest.json` lists `name`, `version`, `size`, and SHA-256 for every publishable
wheel/ZIP and is verified before the build reports success (the manifest does not list itself).

Outputs under `dist/`:

- `xbloom_studio_core-<version>-*.whl` - core library with console entry `xbloom-bridge`
- `knowledge-<version>/` and `knowledge-<version>.zip` - versioned knowledge from the Skill's
  single source (`SKILL.md`, `references/`, `assets/`) plus `manifest.json` (per-file SHA-256 and
  aggregate content hash). Validate with `xbloom_knowledge.validate_bundle(...)` (rejects path
  traversal, missing/tampered files, and extra on-disk knowledge files).
- `skill-xbloom-studio-brew-<version>/` and `.zip` - self-contained Skill with
  `vendor/wheels/` holding that same core wheel, `vendor/release.json` (includes
  `core_wheel_sha256`), and a non-editable `requirements.txt` (exact `==` pins for non-core deps;
  full hash locks deferred per platform — Phase 0.1 incomplete for non-core locks)
- `release-manifest.json` - deterministic per-artifact name/version/size/SHA-256 for the wheel and
  both ZIPs (excludes the manifest itself)

Clean-install check for a release Skill ZIP (extract outside the checkout; do not use the
`dist/skill-.../` tree as a substitute for the published ZIP):

```text
python -c "import zipfile; zipfile.ZipFile('dist/skill-xbloom-studio-brew-<version>.zip').extractall('/tmp/xbloom-skill-clean')"
cd /tmp/xbloom-skill-clean
python scripts/bootstrap.py
python scripts/xbloom.py doctor
python scripts/xbloom.py validate assets/hot-template.yaml
```

Core-only install:

```text
pip install dist/xbloom_studio_core-*.whl
xbloom-bridge --help
```

## Agent installation

Keep the directory name `xbloom-studio-brew`; it must match the `name` in `SKILL.md`.

- Agent Skills clients: place the whole directory in the client's configured skills root.
- Codex: install/copy it under `$CODEX_HOME/skills/xbloom-studio-brew` and restart or reload skills.
- Hermes: install the published Skill directly:

```text
hermes skills install HomoLand/xbloom-studio-brew/skills/xbloom-studio-brew
```

Hermes publishing and custom-repository commands documented by Hermes are:

```text
hermes skills publish xbloom-studio-brew --to github --repo OWNER/REPOSITORY
hermes skills tap add OWNER/REPOSITORY
```

GitHub identifiers include the path to the Skill directory after `OWNER/REPOSITORY`. Test loading with:

```text
hermes chat --toolsets skills,terminal -s xbloom-studio-brew \
  -q "Run the bundled offline catalog status command; do not use BLE or cloud"
```

`skills` lets Hermes load the instructions; `terminal` is separately required for the Agent to
execute the bundled local CLI. Omitting `terminal` can let the model describe a command without
actually running it.

Verify the daemon lifecycle without connecting to hardware:

```text
python scripts/xbloom.py bridge start
python scripts/xbloom.py bridge status
python scripts/xbloom.py bridge stop
```

Hermes exposes the absolute skill directory to the Agent and supports `${HERMES_SKILL_DIR}`, but
this Skill does not depend on that Hermes-only token. Resolve the directory containing `SKILL.md`
and run the scripts from there on every platform.

## Environment configuration

All variables are optional; do not declare owner-gate overrides as automatically required.

| Variable | Purpose |
| --- | --- |
| `XBLOOM_ADDRESS` | Select one machine without scanning; useful when more than one is nearby. |
| `XBLOOM_STATE_DIR` | Canonical state root for `state.db`, bridge discovery/lock, history, catalog, and the default external runtime root. |
| `XBLOOM_SKILL_STATE_DIR` | Legacy alias for the state root; used only when `XBLOOM_STATE_DIR` is unset (supported for v1). |
| `XBLOOM_SKILL_RUNTIME_DIR` | Override only the external Python virtual-environment directory. |
| `XBLOOM_CATALOG_PATH` | Override the private normalized catalog path; default is below the Skill state directory. |
| `XBLOOM_ACCOUNT_EMAIL` | Own-account email for ephemeral `catalog login-sync` or an approved `catalog push --apply`; never commit it. |
| `XBLOOM_ACCOUNT_PASSWORD` | Own-account password for non-interactive login; never pass it as a CLI argument, print it, or commit it. Interactive use has a hidden prompt. |
| `XBLOOM_CLOUD_CONFIG` | Point to an external own-account request form for advanced read-only recipe sync; never commit it. |
| `XBLOOM_ENABLE_REMOTE_START` | Owner opt-in for remote hot-water start; exact sentinel in `device-safety.md`. |
| `XBLOOM_ENABLE_REMOTE_GRINDER` | Separate owner opt-in for the standalone grinder; exact sentinel in `device-safety.md`. |
| `XBLOOM_ENABLE_LIVE_ADJUST` | Separate owner opt-in for FreeSolo live target changes; pattern is hardware-observed only on listed firmware, while temperature write correctness is verified but outlet response is unmeasured. |
| `XBLOOM_ENABLE_SETTINGS_WRITE` | Owner opt-in for persistent unit/display/source and mechanical-tuning writes; exact sentinel in `device-safety.md`. |
| `XBLOOM_ALLOW_UNTESTED_FIRMWARE` | Owner acceptance for an unknown firmware; exact sentinel in `device-safety.md`. |

For Hermes sandboxed execution, explicitly pass through only the variables the deployment needs.
Do not place a BLE address or machine serial in public source control.

Verify account-variable presence without exposing values:

```text
python scripts/xbloom.py doctor
```

`catalog login-sync` defaults to official coffee, official tea, combined user-created, Product/xPod,
and Shared records. `catalog push` is offline preview by default. Remote add requires both `--apply`
and the exact `--confirm-write own-account-cloud-recipe` sentinel; never use the apply path as a
deployment smoke test.

The bridge reads its environment once at launch and records a **config fingerprint** in
`bridge.json`. Client environment changes never silently mutate a running daemon; a fingerprint
mismatch is reported via `hello` / `status` and applies only after an idle restart. Use
`bridge restart-if-idle` (or `xbloom-bridge restart-if-idle`): when the daemon is busy or has
recovery records it returns `upgrade_pending` and does not terminate.

State layout under `~/.xbloom-studio-brew/` (or `XBLOOM_STATE_DIR` / legacy
`XBLOOM_SKILL_STATE_DIR`):

| Path | Role |
| --- | --- |
| `state.db` | SQLite/WAL schema for recipes/workflows/events/idempotency + optional legacy import; **not** yet the active runtime source of truth for catalog/history |
| `bridge.json` | Discovery only: instance id, pid, loopback host/port, token, core/protocol/record-format versions, config fingerprint |
| `bridge.lock` | Lifecycle OS lock (one daemon per state root); not discovery |
| `bridge.log` | Daemon stdout/stderr |
| legacy `catalog/`, `brew-history.jsonl`, `*-state.json` | Still written by runtime; optional explicit import via `state migrate` / `xbloom-state migrate` (originals kept) |

### Explicit state migration (no auto-migrate on daemon start)

```text
python scripts/xbloom.py state status
python scripts/xbloom.py state migrate
python scripts/xbloom.py state backup
# core: xbloom-state status|migrate|backup
```

Migration is idempotent and observable. While catalog/history remain JSON-backed, status
always reports `runtime_source_of_truth: json_legacy` and `sqlite_active_runtime: false`.
Do not treat a completed migration receipt as cutover. Bridge wire protocol is **v2**
(required hello + envelope); config fingerprint includes the effective BLE address.

Never publish tokens or private state. The server binds to loopback, requires the token on every
JSON-line request, rejects incompatible clients before BLE writes (`hello` + protocol range),
serializes BLE writes, and holds at most one Studio connection. This is local process isolation,
not remote authentication.

Daemon lifecycle is core-owned (`python -m xbloom_ble.bridge` / `xbloom-bridge`); Skill
`bridge start|serve|stop` call the same APIs and do not require a Skill script path to spawn the
child.

## Publication layout

Publish the entire directory, including:

- `SKILL.md`, `agents/openai.yaml`, `scripts/`, `references/`, and `assets/`.
- `requirements*.txt` for reproducible runtime setup.
- `LICENSE`, `THIRD_PARTY_NOTICES.md`, and the two license texts under `licenses/`.
- `tests/` so downstream users can audit the reverse-engineered protocol before connecting.

Do not commit a virtual environment, telemetry captures, machine addresses, armed-state files,
cloud tokens, or recipes containing private purchase/account data.

## Release checklist

1. Run `python scripts/bootstrap.py --dev` on a clean checkout.
2. Run `python tools/build_release.py` from the repository root; confirm `dist/` contains the core
   wheel, knowledge bundle (valid `manifest.json`), Skill bundle with `vendor/wheels/`, and a
   verified `release-manifest.json`. Confirm two consecutive builds with the same
   `SOURCE_DATE_EPOCH` produce matching SHA-256 for the wheel and both ZIPs.
3. In a clean venv, install the built core wheel and invoke `xbloom-bridge --help`.
4. Extract `dist/skill-xbloom-studio-brew-<version>.zip` into a fresh temporary directory **outside**
   the checkout (not the `dist/skill-.../` tree), run `python scripts/bootstrap.py`, then `doctor`
   and `validate` without a sibling `packages/core` checkout. Confirm `vendor/release.json`
   `core_wheel_sha256` matches the vendored wheel.
5. Run the Agent Skills structural validator.
6. Inspect `git diff` for addresses, serials, tokens, and packet captures.
7. Import scripted coffee, tea, Easy, xPod, and J20 JSON fixtures; mock all five own-account recipe
   categories and the add endpoint; confirm secrets/raw responses are absent from the saved catalog,
   push is preview-only by default, and exported YAML passes the guarded validator. Never mutate a
   live account as part of release tests.
8. Confirm coffee and tea load frames exclude their execute/start commands.
9. Test `doctor`, `scan`, and `probe` on each supported OS when available.
10. Test `scale` with the platform empty; confirm `entering → ready → exited`, then place a known
   object only after `ready` and verify its reading. Treat `--tare` as an additional re-tare.
11. For a supported firmware, load a conservative recipe and then cancel without starting.
12. Pin RT's offline frame encoding to the app's 20 C sentinel, but keep grinder, water, coffee
   start, and tea start out of unattended release tests.
13. Run `bridge start`, `bridge status`, and idle `bridge stop` with no hardware connection. Confirm
   every one-shot BLE command refuses while the bridge owns the local control endpoint.
14. Test bridge state transitions for coffee, tea, scale, grinder, water, presets, settings, and
    advanced tuning against scripted BLE only. Keep grinder/water/coffee/tea actuation and FreeSolo
    live-target commands out of unattended tests; record supervised hardware evidence separately.
15. Pin persistent settings and advanced-tuning command frames/readbacks in fake-BLE tests. Do not
    run those writes as an unattended release check; record supervised results separately.
16. Verify telemetry labels recipe target, cumulative machine output, and cup-scale delta
    independently, without claiming water-supply inventory.
17. Verify `validate --slot` and every slot-writing layer reject bypass before BLE resolution.
18. Never add firmware to the allowlist based only on a successful scan.
19. Tag the release, attach `dist/` artifacts to the GitHub Release, and record the vendored
    upstream commit in `THIRD_PARTY_NOTICES.md`.

## Architecture boundary

The package remains a portable Skill, but now includes a small long-lived BLE bridge for the cases
that cannot be made safe with one-shot processes. It owns one connection, maintains a state
machine, serializes writes, exposes token-authenticated loopback JSON-line RPC through the same CLI,
and retains bounded event history. Coffee, tea, scale, grinder, FreeSolo water, presets, settings,
and advanced tuning all use this path when the bridge is running.

This bridge is deliberately not a LAN service, cloud relay, account connector, or general raw BLE
socket. The separate catalog module may make bounded own-account reads and an explicitly gated,
idempotent add-only recipe write, but never passes credentials into the bridge, persists the login
session, or makes cloud access a dependency of BLE. Concurrent remote Agents, cross-host
authentication, high-rate binary streaming, and
multi-user authorization would justify a separately secured native Tool/MCP service. Keep the
recipe and physical-safety workflow in this Skill even if such a transport is added later.

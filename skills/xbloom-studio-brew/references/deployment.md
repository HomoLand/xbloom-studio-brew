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
  terminal backend or expose a separately secured local tool instead.

No xBloom account, cloud token, Android app credential, or internet connection is required after
the Python dependencies are installed.

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

From the skill directory, create its isolated runtime:

```text
python scripts/bootstrap.py
```

For contributors, also install test dependencies and run the test suite:

```text
python scripts/bootstrap.py --dev
```

The bootstrap creates `.venv` inside the skill directory, installs pinned dependencies, runs the
doctor check, and, with `--dev`, runs tests. The main CLI automatically re-executes inside this
runtime, so Agents can consistently call `python scripts/xbloom.py ...`.

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
hermes chat --toolsets skills -q "Use xbloom-studio-brew to validate a hot recipe"
```

Hermes exposes the absolute skill directory to the Agent and supports `${HERMES_SKILL_DIR}`, but
this Skill does not depend on that Hermes-only token. Resolve the directory containing `SKILL.md`
and run the scripts from there on every platform.

## Environment configuration

All variables are optional; do not declare the two safety overrides as automatically required.

| Variable | Purpose |
| --- | --- |
| `XBLOOM_ADDRESS` | Select one machine without scanning; useful when more than one is nearby. |
| `XBLOOM_SKILL_STATE_DIR` | Relocate the short-lived armed-state record. |
| `XBLOOM_ENABLE_REMOTE_START` | Owner opt-in for remote hot-water start; exact sentinel in `device-safety.md`. |
| `XBLOOM_ENABLE_REMOTE_GRINDER` | Separate owner opt-in for the standalone grinder; exact sentinel in `device-safety.md`. |
| `XBLOOM_ALLOW_UNTESTED_FIRMWARE` | Owner acceptance for an unknown firmware; exact sentinel in `device-safety.md`. |

For Hermes sandboxed execution, explicitly pass through only the variables the deployment needs.
Do not place a BLE address or machine serial in public source control.

## Publication layout

Publish the entire directory, including:

- `SKILL.md`, `agents/openai.yaml`, `scripts/`, `references/`, and `assets/`.
- `requirements*.txt` for reproducible runtime setup.
- `LICENSE`, `THIRD_PARTY_NOTICES.md`, and the two license texts under `licenses/`.
- `tests/` so downstream users can audit the reverse-engineered protocol before connecting.

Do not commit `.venv`, telemetry captures, machine addresses, armed-state files, cloud tokens, or
recipes containing private purchase/account data.

## Release checklist

1. Run `python scripts/bootstrap.py --dev` on a clean checkout.
2. Run the Agent Skills structural validator.
3. Inspect `git diff` for addresses, serials, tokens, and packet captures.
4. Confirm coffee and tea load frames exclude their execute/start commands.
5. Test `doctor`, `scan`, and `probe` on each supported OS when available.
6. Test `scale` with the platform empty; confirm `entering → ready → exited`, then place a known
   object only after `ready` and verify its reading. Treat `--tare` as an additional re-tare.
7. For a supported firmware, load a conservative recipe and then cancel without starting.
8. Pin RT's offline frame encoding to the app's 20 C sentinel, but keep grinder, water, coffee
   start, and tea start out of unattended release tests.
9. Never add firmware to the allowlist based only on a successful scan.
10. Tag the release and record the vendored upstream commit in `THIRD_PARTY_NOTICES.md`.

## Architecture boundary

Hermes documentation notes that binary streaming and precise real-time integration can justify a
native Tool. This package deliberately remains a portable Skill by putting those details behind a
small CLI that emits JSON. If a future deployment needs a persistent BLE daemon, concurrent Agents,
or remote-device authentication, build that as a separately secured local Tool/MCP server and keep
this recipe/safety workflow as the shared Skill layer.

# xbloom-studio-core

Shared foundation for xBloom Studio Brew: BLE protocol, recipe validation, catalog,
history, transactional SQLite storage, path helpers, knowledge-bundle validation,
and the loopback BLE bridge daemon.

Install from a built wheel (GitHub Releases) or editable from a checkout:

```text
pip install dist/xbloom_studio_core-*.whl
# development
pip install -e packages/core
```

## State directory

| Variable | Role |
| --- | --- |
| `XBLOOM_STATE_DIR` | Canonical state root (normalised). Default `~/.xbloom-studio-brew`. |
| `XBLOOM_SKILL_STATE_DIR` | Legacy alias; used only when `XBLOOM_STATE_DIR` is unset. |
| `XBLOOM_SKILL_RUNTIME_DIR` | External virtualenv root (default `<state>/runtime`). |

Under the state root:

- `state.db` - SQLite/WAL workflows, history, idempotency, recipe revisions schema; catalog cutover pending
- `bridge.json` - discovery record (instance, port, token, versions); not a lock
- `bridge.lock` - lifecycle OS lock (fcntl / msvcrt); one daemon per state root
- legacy JSON/JSONL files can be imported **explicitly** via `xbloom-state migrate`
  / `xbloom_storage.migrate_legacy_state` (not on daemon startup)

### State migration transition contract

| Command | Effect |
| --- | --- |
| `xbloom-state status` | Migration receipt + declares runtime source of truth |
| `xbloom-state migrate` | Timestamped backup of legacy files, import into `state.db` (idempotent) |
| `xbloom-state backup` | Online SQLite backup of `state.db` only |

**Partial runtime cutover (history complete; catalog pending).** SQLite/WAL is the
authoritative runtime store for workflow, brew history (`history_events`), and
idempotency. Catalog writers remain JSON-backed until catalog cutover. A completed
migration receipt means legacy snapshots (including brew-history lines) were imported
into `state.db`; it is **not** full catalog cutover. Do not auto-migrate on bridge
daemon start. Runtime history never rewrites `brew-history.jsonl`.

Skill mirror: `python scripts/xbloom.py state status|migrate|backup`.

### Recipe / revision API (Phase B B8)

`StateStore` is the authoritative SQLite recipe store for Web B9. High-level APIs
validate with core domain rules before writing:

| Method | Contract |
| --- | --- |
| `create_recipe_with_revision` | Atomic recipe + first immutable revision |
| `create_recipe_revision` | New revision with mandatory `expected_parent_revision_id` OCC |
| `get_latest_recipe_revision` / `list_recipe_revisions` | Read path |
| `list_recipes` | Latest revision summary/content, kind/query filters, pagination, archive flag |
| `archive_recipe` / `restore_recipe` | Soft archive; optional expected-latest revision guard |

Raises `StorageConflictError` on stale parent/latest revision. Provenance rejects
raw image material, secrets, chain-of-thought, local paths, nested forbidden keys,
and binary payloads (never stripped silently). Safe boolean image-use facts such as
`used_image` / `image_present` are allowed. Trusted lineage fields are not
spoofable: stored `parent_revision_id` always matches the real DB parent (omitted
on the first revision), and an explicit `creation_source` method argument overrides
caller provenance. Low-level `upsert_recipe` / `add_recipe_revision` remain for
legacy import and bridge-internal use without domain re-validation.

## Console entry

```text
xbloom-bridge serve [--address <ble-id>]   # foreground daemon (default)
xbloom-bridge start
xbloom-bridge status
xbloom-bridge stop [--force]
xbloom-bridge restart-if-idle
# also: python -m xbloom_ble.bridge ...

xbloom-state status
xbloom-state migrate [--force] [--backup-root DIR]
xbloom-state backup [--destination PATH]
# also: python -m xbloom_storage ...
```

Core lifecycle helpers: `ensure_bridge_daemon`, `start_bridge_daemon`,
`stop_bridge_daemon`, `restart_bridge_daemon_if_idle` (no Skill script path).

Wire protocol is **v2** (required `hello` + RPC envelope). Record format version is tracked
separately (`record_format_version`). Legacy v1 daemons are detected; idle ones can be
token-shut down and replaced; active/recovery ones report `upgrade_pending` and are never
force-stopped.

This package is not published to PyPI. Release artifacts are built by
`tools/build_release.py` at the repository root.

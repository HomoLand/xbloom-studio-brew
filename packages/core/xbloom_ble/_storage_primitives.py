"""Transactional SQLite storage for workflows, history, and catalog schema.

Phase 0 baseline: schema, primitives, integrity, online backup, and a one-time
lossless import of legacy JSON/JSONL state. Phase 0.3/0.4 history and catalog
cutovers make ``history_events`` and ``recipes``/``recipe_revisions`` the
authoritative runtime stores (legacy JSON/JSONL are import-only). Phase B B8
adds typed recipe/revision APIs with optimistic concurrency, domain validation,
and safe provenance. Physical BLE workflow semantics remain in the bridge.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from xbloom_ble.recipe import Recipe, RecipeError
from xbloom_ble.tea import TeaRecipe, TeaRecipeError
from xbloom_paths import normalize_state_root, state_dir as resolve_state_dir
from xbloom_safety import SafetyError, strict_validate

SCHEMA_VERSION = 5
DB_FILE_NAME = "state.db"
BUSY_TIMEOUT_MS = 5000
# Cross-connection fresh-DB bootstrap: exclusive lock + busy retries (not a
# process-global mutex). Windows WAL create races commonly need several attempts.
SCHEMA_INIT_MAX_ATTEMPTS = 40
SCHEMA_INIT_BASE_DELAY_S = 0.01
DEFAULT_BACKUP_DIRNAME = "backups"
LEGACY_MIGRATION_NAME = "legacy_json_v1"
# Independent of legacy_json_v1: backfills history_events from legacy_imports
# (or from the same import transaction on first cutover). Required so schema-v3
# installs that already hold a legacy_json_v1 receipt still populate the v4
# journal on a normal migrate without --force.
LEGACY_HISTORY_CUTOVER_NAME = "legacy_history_sqlite_v1"
# Independent catalog cutover: recipes + recipe_revisions are the single
# authoritative local catalog. Existing installs with only legacy_json_v1
# (and possibly history cutover) still get a full catalog backfill from
# legacy_imports on a normal migrate without --force.
LEGACY_CATALOG_CUTOVER_NAME = "legacy_catalog_sqlite_v1"
DEFAULT_RECIPE_LIST_LIMIT = 50
MAX_RECIPE_LIST_LIMIT = 500
DEFAULT_HISTORY_LIST_LIMIT = 20
MAX_HISTORY_LIST_LIMIT = 200
MAX_HISTORY_NOTE_CHARS = 500
HISTORY_SOURCE_LOCAL = "local-skill"
HISTORY_SOURCE_APP = "app-cloud"
CATALOG_SOURCE_LEGACY = "legacy_catalog"
CATALOG_SOURCE_SKILL = "skill-catalog"
CATALOG_SOURCE_WEB = "web"
CATALOG_SOURCE_MERGE = "catalog-merge"
# Runtime view fields injected by build_catalog_snapshot; never persist into
# metadata.catalog_envelope (load -> save must not pollute or churn).
CATALOG_ENVELOPE_RUNTIME_KEYS = frozenset(
    {
        "recipe",
        "recipe_id",
        "catalog_owned",
        "derived",
        "archived_at",
        "id",
    }
)
HISTORY_VALID_OUTCOMES = frozenset(
    {
        "loaded",
        "started",
        "completed",
        "completion_unconfirmed",
        "cancelled",
        "failed",
        "imported",
    }
)
# Strip accidental secrets from public history event shapes.
_HISTORY_STRIP_KEYS = frozenset(
    {"password", "token", "clientSecretStr", "session"}
)

# Non-terminal workflow states that may still own recovery / connection scope.
ACTIVE_WORKFLOW_STATES = frozenset(
    {
        "created",
        "loading",
        "loaded",
        "load_unconfirmed",
        "starting",
        "running",
        "paused",
        "soaking",
        "stopping",
        "control_unconfirmed",
        "stop_unconfirmed",
        "recovery",
        "recovery_required",
        "recovering",
        "recovery_imported",
    }
)

# Validated workflow kinds for structured queries (no ad hoc SQL outside storage).
KNOWN_WORKFLOW_KINDS = frozenset(
    {
        "coffee",
        "tea",
        "grinder",
        "water",
        "scale",
        "settings",
        "advanced",
        "presets",
        "coffee_recovery",
        "tea_recovery",
        "grinder_recovery",
    }
)
GRINDER_WORKFLOW_KINDS = frozenset({"grinder", "grinder_recovery"})
RECOVERY_WORKFLOW_KINDS = frozenset(
    {"coffee_recovery", "tea_recovery", "grinder_recovery"}
)
# Legacy grinder-rest JSON status/phase values that mean confirmed stop / rest.
LEGACY_GRINDER_TERMINAL_STATUSES = frozenset(
    {
        "rest",
        "stopped",
        "complete",
        "completed",
        "idle",
        "cancelled",
    }
)

# Idempotency row lifecycle.
IDEM_PENDING = "pending"
IDEM_COMPLETED = "completed"
IDEM_FAILED = "failed"

# Explicit recovery_json contract for workflow updates / terminal commits:
# - omit / pass ``None`` -> preserve existing recovery_json
# - pass ``CLEAR_RECOVERY`` -> set recovery_json to NULL (true clear)
# - pass a mapping -> replace recovery_json with that payload
class _ClearRecovery:
    """Sentinel type: clear recovery_json rather than preserve it."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CLEAR_RECOVERY"


CLEAR_RECOVERY = _ClearRecovery()

CATALOG_REL = Path("catalog") / "catalog.json"
HISTORY_REL = Path("brew-history.jsonl")
ARMED_STATE_REL = Path("armed-state.json")
TEA_LOADED_STATE_REL = Path("tea-loaded-state.json")
GRINDER_REST_STATE_REL = Path("grinder-rest-state.json")

LEGACY_SOURCES: tuple[tuple[str, Path], ...] = (
    ("catalog", CATALOG_REL),
    ("history", HISTORY_REL),
    ("recovery_armed", ARMED_STATE_REL),
    ("recovery_tea", TEA_LOADED_STATE_REL),
    ("recovery_grinder", GRINDER_REST_STATE_REL),
)

# Injected failure hooks for tests: callable taking stage name, may raise.
_migration_fault_hook: Any = None
_recipe_write_fault_hook: Any = None

# Provenance keys rejected at high-level recipe APIs (semantic token match).
# Low-level upsert_recipe / add_recipe_revision remain permissive for legacy
# import and bridge-internal writes that need extensible fields.
#
# Image *material* is forbidden (raw bytes, base64, paths, bare image/photo
# fields). Harmless image *facts* such as used_image / image_present (booleans)
# are allowed because Web B9 needs them for design-service provenance.
_FORBIDDEN_PROVENANCE_KEYS = frozenset(
    {
        "image",
        "images",
        "image_base64",
        "image_bytes",
        "image_data",
        "image_payload",
        "photo",
        "photos",
        "api_key",
        "apikey",
        "api_token",
        "token",
        "access_token",
        "refresh_token",
        "session_token",
        "password",
        "secret",
        "authorization",
        "auth_header",
        "chain_of_thought",
        "reasoning",
        "reasoning_content",
        "raw_reasoning",
        "thinking",
        "cot",
        "raw_thinking",
        "raw_image",
        "path",
        "file_path",
        "filepath",
        "local_path",
        "local_file",
        "image_path",
        "recipe_path",
        "source_path",
    }
)
# Single semantic tokens that are always forbidden when present as whole words
# after snake/kebab/space/camelCase splitting (not substring matches).
# "image"/"photo" tokens are handled separately via an allowlist so safe
# scalar metadata (used_image, image_present) is not a false positive while
# raw forms (image_base64, raw_image, bare image, etc.) stay rejected.
_FORBIDDEN_PROVENANCE_TOKENS = frozenset(
    {
        "password",
        "secret",
        "token",
        "authorization",
        "reasoning",
        "thinking",
        "cot",
        "path",
        "filepath",
    }
)
# Image/photo material tokens: forbidden unless the full key is a safe
# image-use metadata fact (see _SAFE_IMAGE_METADATA_KEYS).
_IMAGE_MATERIAL_TOKENS = frozenset(
    {
        "image",
        "images",
        "photo",
        "photos",
    }
)
# Adjacent token pairs treated as one forbidden concept (raw image material).
_FORBIDDEN_PROVENANCE_TOKEN_PAIRS = frozenset(
    {
        ("api", "key"),
        ("api", "token"),
        ("access", "token"),
        ("refresh", "token"),
        ("session", "token"),
        ("auth", "header"),
        ("image", "base64"),
        ("image", "bytes"),
        ("image", "data"),
        ("image", "payload"),
        ("image", "path"),
        ("raw", "image"),
        ("raw", "thinking"),
        ("raw", "reasoning"),
        ("file", "path"),
        ("local", "path"),
        ("local", "file"),
        ("recipe", "path"),
        ("source", "path"),
        ("chain", "thought"),  # after dropping filler "of"
    }
)
# Safe image-use metadata keys (boolean facts only, not material).
_SAFE_IMAGE_METADATA_KEYS = frozenset(
    {
        "used_image",
        "image_present",
    }
)
_CAMEL_BOUNDARY_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+"
)
_PROVENANCE_BINARY_TYPES = (bytes, bytearray, memoryview)
_ALTER_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE | re.DOTALL,
)


class StorageError(RuntimeError):
    """Raised for storage, migration, or integrity failures."""


class StorageConflictError(StorageError):
    """Optimistic concurrency conflict (stale parent / expected revision)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    """Stable JSON encoding for hashing and durable storage."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))


def new_history_event_id() -> str:
    return f"bh_{uuid4().hex}"


def history_event_dedupe_key(event_id: str) -> str:
    """Runtime / retry dedupe key for a public event_id."""

    return f"event:{event_id}"


def workflow_terminal_history_dedupe_key(workflow_id: str) -> str:
    """Idempotent key for the one final history row per workflow terminal."""

    return f"workflow:{workflow_id}:terminal"


def legacy_history_line_dedupe_key(line_no: int, line_digest: str) -> str:
    """Preserve each JSONL source line as its own row (even duplicate event_id)."""

    return legacy_history_record_dedupe_key(f"line:{line_no}:{line_digest}")


def legacy_history_record_dedupe_key(record_key: str) -> str:
    """Dedupe key for a legacy_imports history row (source identity / record_key)."""

    return f"legacy:{record_key}"


def recipe_id_for_catalog_entry_id(entry_id: str) -> str:
    """Deterministic stable recipe_id for a public catalog entry id.

    Matches the Phase 0 legacy import mapping so existing migrated IDs remain
    resolvable after catalog cutover.
    """

    text = str(entry_id).strip()
    if not text:
        raise StorageError("catalog entry id must be a non-empty string")
    return f"legacy_{text}"


def catalog_entry_id_from_recipe_id(recipe_id: str) -> str | None:
    """Inverse of :func:`recipe_id_for_catalog_entry_id` when applicable.

    A ``legacy_`` prefix alone does **not** imply catalog ownership. Use
    :func:`catalog_ownership_entry_ids` for ownership checks.
    """

    text = str(recipe_id or "")
    if text.startswith("legacy_"):
        return text[len("legacy_") :]
    return None


def catalog_ownership_entry_ids(
    metadata: Mapping[str, Any] | None,
    provenance: Mapping[str, Any] | None,
) -> set[str]:
    """Explicit catalog-ownership markers only (never recipe_id prefix alone).

    Legitimate catalog / partial-legacy rows are identified by matching
    ``metadata.catalog_entry_id`` or ``provenance.catalog_entry_id`` /
    ``provenance.legacy_entry_id``.
    """

    owned: set[str] = set()
    meta = metadata if isinstance(metadata, Mapping) else {}
    prov = provenance if isinstance(provenance, Mapping) else {}
    for value in (
        meta.get("catalog_entry_id"),
        prov.get("catalog_entry_id"),
        prov.get("legacy_entry_id"),
    ):
        text = str(value).strip() if value is not None else ""
        if text:
            owned.add(text)
    return owned


def normalize_catalog_envelope(envelope: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a catalog envelope with runtime view fields stripped."""

    if not isinstance(envelope, Mapping):
        return {}
    return {
        key: value
        for key, value in envelope.items()
        if key not in CATALOG_ENVELOPE_RUNTIME_KEYS
    }


def split_catalog_entry(entry: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a normalized catalog entry into envelope (no recipe) + recipe body.

    Runtime snapshot fields (``recipe_id``, ``catalog_owned``, ``derived``,
    ``archived_at``) are stripped so they are never persisted into
    ``metadata.catalog_envelope``.
    """

    if not isinstance(entry, Mapping):
        raise StorageError("catalog entry must be a mapping")
    data = dict(entry)
    recipe = data.pop("recipe", None)
    if recipe is None:
        recipe = {}
    if not isinstance(recipe, Mapping):
        raise StorageError("catalog entry recipe must be an object mapping")
    return normalize_catalog_envelope(data), dict(recipe)


def merge_catalog_envelopes(
    existing: Mapping[str, Any] | None,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge catalog entry envelopes (sources/slots union; incoming wins fields)."""

    base = normalize_catalog_envelope(existing)
    merged = normalize_catalog_envelope(incoming)
    merged["first_seen_at"] = base.get("first_seen_at", merged.get("first_seen_at"))
    old_sources = list(base.get("sources") or [])
    new_sources = list(merged.get("sources") or [])
    source_map: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
    for source in [*old_sources, *new_sources]:
        if not isinstance(source, dict):
            continue
        key = (
            source.get("type"),
            source.get("endpoint"),
            source.get("file"),
            source.get("region"),
        )
        source_map[key] = source
    merged["sources"] = sorted(
        source_map.values(),
        key=lambda item: (
            str(item.get("type")),
            str(item.get("endpoint")),
            str(item.get("file")),
        ),
    )
    slots: dict[str, dict[str, Any]] = {}
    for slot in [*(base.get("slots") or []), *(merged.get("slots") or [])]:
        if isinstance(slot, dict) and slot.get("position"):
            slots[str(slot["position"]).upper()] = slot
    merged["slots"] = [slots[key] for key in sorted(slots)]
    return merged


def public_history_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a public history event dict with accidental secrets stripped."""

    data = dict(event)
    for key in _HISTORY_STRIP_KEYS:
        data.pop(key, None)
    return data


def _history_clean_text(
    value: Any, *, field: str, max_chars: int = 240
) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise StorageError(f"{field} must be at most {max_chars} characters")
    return text


def map_terminal_history_outcome(
    state: str, payload: Mapping[str, Any] | None = None
) -> str:
    """Map a durable workflow terminal state to a public history outcome."""

    payload = dict(payload or {})
    raw = str(state or "").strip()
    lowered = raw.casefold()
    if raw in HISTORY_VALID_OUTCOMES:
        return raw
    if lowered in HISTORY_VALID_OUTCOMES:
        return lowered
    if lowered in {
        "ready",
        "completed",
        "done",
        "exited",
        "finished",
        "complete",
        "saved",
        "written_and_read_back",
    }:
        return "completed"
    if lowered in {"cancelled", "canceled", "cancel", "cancel_sent", "stopped"}:
        return "cancelled"
    if lowered in {"failed", "error", "load_failed", "fault"}:
        return "failed"
    if "unconfirmed" in lowered:
        return "completion_unconfirmed"
    if payload.get("emergency"):
        return "cancelled"
    if "cancel" in lowered or "stop" in lowered:
        return "cancelled"
    if "fail" in lowered or "error" in lowered:
        return "failed"
    # Machine natural terminal names (ready/etc.) already handled; default failed.
    return "failed"


def history_event_from_workflow_terminal(
    workflow_row: Mapping[str, Any],
    *,
    state: str,
    event_payload: Mapping[str, Any] | None = None,
    terminal_at: str,
) -> dict[str, Any]:
    """Build one public history event from an immutable workflow terminal commit."""

    payload = dict(event_payload or {})
    snapshot = workflow_row.get("snapshot")
    if snapshot is None and workflow_row.get("snapshot_json"):
        try:
            snapshot = json.loads(workflow_row["snapshot_json"])
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    metadata = workflow_row.get("metadata")
    if metadata is None and workflow_row.get("metadata_json") is not None:
        try:
            metadata = json.loads(workflow_row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    workflow_id = str(workflow_row.get("workflow_id") or "")
    kind = workflow_row.get("kind")
    outcome = map_terminal_history_outcome(state, payload)
    source = str(workflow_row.get("source") or HISTORY_SOURCE_LOCAL).strip() or (
        HISTORY_SOURCE_LOCAL
    )
    # snapshot_sha256 is the content hash of the workflow snapshot row.
    # recipe_sha256 is the recipe/source content hash when known; revision-only
    # recipes may omit it while snapshot_sha256 remains present. Never mislabel
    # the snapshot digest as recipe_sha256.
    snapshot_sha = workflow_row.get("snapshot_sha256")
    recipe_sha = (
        snapshot.get("_source_sha256")
        or snapshot.get("recipe_sha256")
        or metadata.get("recipe_sha256")
        or metadata.get("_source_sha256")
    )
    event: dict[str, Any] = {
        "event_kind": "workflow",
        "outcome": outcome,
        "source": source,
        "workflow_id": workflow_id,
        "recipe_revision_id": workflow_row.get("recipe_revision_id"),
        "snapshot_sha256": snapshot_sha,
        "recipe_sha256": recipe_sha,
        "recipe_name": (
            snapshot.get("name")
            or metadata.get("recipe_name")
            or snapshot.get("recipe_name")
        ),
        "recipe_path": metadata.get("recipe_path") or snapshot.get("recipe_path"),
        "kind": kind,
        "serving_kind": (
            snapshot.get("kind")
            or snapshot.get("serving_kind")
            or metadata.get("serving_kind")
            or kind
        ),
        "machine_program": (
            snapshot.get("machine_program") or metadata.get("machine_program")
        ),
        "result": payload.get("result", state),
        "terminal_at": terminal_at,
        "recorded_at": terminal_at,
        "machine": payload.get("machine") or metadata.get("machine"),
        "address": payload.get("address") or metadata.get("address"),
        "firmware": payload.get("firmware") or metadata.get("firmware"),
        "activity": payload.get("activity"),
        "release_reason": payload.get("release_reason"),
        "dose_g": snapshot.get("dose_g") or snapshot.get("leaf_g"),
        "grind": snapshot.get("grind"),
        "pours": snapshot.get("pours") or snapshot.get("steeps"),
        "target_dispensed_water_ml": (
            payload.get("target_dispensed_water_ml")
            or snapshot.get("target_dispensed_water_ml")
            or snapshot.get("programmed_water_ml")
        ),
        "dispensed_water_ml": payload.get("dispensed_water_ml"),
        "cup_delta_g": payload.get("cup_delta_g"),
        "emergency": payload.get("emergency"),
        "error": payload.get("error"),
    }
    # Drop empty optional fields for a compact public journal row.
    return {
        key: value
        for key, value in event.items()
        if value not in (None, "", [], {})
    }


def default_db_path(state_root: Path | None = None) -> Path:
    root = normalize_state_root(state_root) if state_root is not None else resolve_state_dir()
    return root / DB_FILE_NAME


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _maybe_recipe_write_fault(stage: str) -> None:
    import sys

    storage_mod = sys.modules.get("xbloom_storage")
    hook = getattr(storage_mod, "_recipe_write_fault_hook", _recipe_write_fault_hook)
    if hook is not None:
        hook(stage)


def _is_sqlite_locked_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _provenance_key_tokens(key: str) -> list[str]:
    """Split a provenance key into semantic tokens (snake/kebab/space/camelCase)."""

    tokens: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", str(key)):
        if not chunk:
            continue
        pieces = _CAMEL_BOUNDARY_RE.findall(chunk)
        if pieces:
            tokens.extend(piece.lower() for piece in pieces)
        else:
            tokens.append(chunk.lower())
    return tokens


def _is_safe_image_metadata_key(key: str) -> bool:
    """Return True for boolean image-use facts such as used_image."""

    tokens = _provenance_key_tokens(key)
    if not tokens:
        return False
    joined = "_".join(tokens)
    compact = "".join(tokens)
    return joined in _SAFE_IMAGE_METADATA_KEYS or compact in _SAFE_IMAGE_METADATA_KEYS


def _is_forbidden_provenance_key(key: str) -> bool:
    """Return True when *key* names a forbidden provenance concept.

    Uses whole-token semantics so ``tokenizer`` and ``pathway`` are not
    rejected as false positives for ``token`` / ``path``. Safe image-use
    metadata keys (``used_image``, ``image_present``) are not forbidden;
    any other key with an image/photo token is treated as raw material.
    """

    tokens = _provenance_key_tokens(key)
    if not tokens:
        return False
    if _is_safe_image_metadata_key(key):
        return False
    # Any non-allowlisted key containing image/photo material tokens is forbidden.
    if any(token in _IMAGE_MATERIAL_TOKENS for token in tokens):
        return True
    joined = "_".join(tokens)
    compact = "".join(tokens)
    if joined in _FORBIDDEN_PROVENANCE_KEYS or compact in _FORBIDDEN_PROVENANCE_KEYS:
        return True
    if any(token in _FORBIDDEN_PROVENANCE_TOKENS for token in tokens):
        return True
    # Drop common filler words for multi-token phrases (chain of thought).
    core = [t for t in tokens if t not in {"of", "the", "a", "an"}]
    for index in range(len(core) - 1):
        if (core[index], core[index + 1]) in _FORBIDDEN_PROVENANCE_TOKEN_PAIRS:
            return True
    for index in range(len(tokens) - 1):
        if (tokens[index], tokens[index + 1]) in _FORBIDDEN_PROVENANCE_TOKEN_PAIRS:
            return True
    return False


def reject_forbidden_provenance(value: Any, *, path: str = "provenance") -> None:
    """Raise StorageError if *value* contains forbidden provenance fields.

    Rejects raw image / secret / token / reasoning / local-path keys recursively
    (snake_case, kebab-case, spaces, camelCase) and binary payload values
    (bytes, bytearray, memoryview) even under neutral keys. Never strips silently.

    Safe image-use facts (``used_image``, ``image_present``) are accepted only
    as JSON scalars; raw image material keys remain forbidden.
    """

    if isinstance(value, _PROVENANCE_BINARY_TYPES):
        raise StorageError(f"forbidden binary provenance payload at {path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_s = str(key)
            child_path = f"{path}.{key_s}"
            if _is_forbidden_provenance_key(key_s):
                raise StorageError(
                    f"forbidden provenance field {key_s!r} at {child_path}"
                )
            if _is_safe_image_metadata_key(key_s) and not isinstance(child, bool):
                raise StorageError(
                    f"forbidden non-boolean image metadata at {child_path}"
                )
            reject_forbidden_provenance(child, path=child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            reject_forbidden_provenance(child, path=f"{path}[{index}]")


def sanitize_recipe_provenance(
    provenance: Mapping[str, Any] | None = None,
    *,
    parent_revision_id: str | None = None,
    creation_source: str | None = None,
) -> dict[str, Any]:
    """Return a safe, JSON-ready provenance dict for high-level recipe APIs.

    Allowed extensible fields pass through when harmless. Forbidden keys raise.

    Trusted lineage is never spoofable by caller provenance:

    - ``parent_revision_id`` is always forced from the trusted argument.
      Pass the real DB parent for edits; pass ``None`` for a first revision
      so a forged parent is omitted (never preserved).
    - When ``creation_source`` is supplied as an explicit argument, it
      overrides any conflicting value in *provenance*.
    """

    if provenance is not None and not isinstance(provenance, Mapping):
        raise StorageError("provenance must be a mapping")
    out = dict(provenance or {})
    # Explicit method-arg creation_source always wins over caller provenance.
    if creation_source is not None:
        out["creation_source"] = creation_source
    # Trusted parent always wins: set real parent, or drop a forged first-rev parent.
    if parent_revision_id is not None:
        out["parent_revision_id"] = parent_revision_id
    else:
        out.pop("parent_revision_id", None)
    reject_forbidden_provenance(out)
    return out


def _tea_to_canonical_dict(recipe: TeaRecipe) -> dict[str, Any]:
    pours: list[dict[str, Any]] = []
    for pour in recipe.pours:
        item: dict[str, Any] = {
            "ml": int(pour.ml),
            "temp_c": int(pour.temp_c),
            "pattern": pour.pattern,
            "pause_s": int(pour.pause_s),
            "flow_ml_s": float(pour.flow_ml_s),
        }
        if pour.label is not None:
            item["label"] = pour.label
        pours.append(item)
    return {
        "name": recipe.name,
        "kind": "tea",
        "leaf_g": float(recipe.leaf_g),
        "output_ml_per_steep": int(recipe.output_ml_per_steep),
        "pours": pours,
    }


def canonicalize_recipe_content(
    content: Any,
) -> tuple[dict[str, Any], str]:
    """Validate and canonicalize recipe content using core domain rules.

    Returns ``(canonical_content, storage_kind)`` where *storage_kind* is
    ``\"coffee\"`` or ``\"tea\"`` (catalog kind, not coffee serving kind).

    Coffee: :meth:`Recipe.from_dict` + :func:`strict_validate`.
    Tea: :meth:`TeaRecipe.from_dict` (includes validate).
    Rejects file paths and non-mapping content before any storage write.
    """

    if isinstance(content, (str, Path)):
        raise StorageError(
            "recipe content must be a JSON object mapping, not a local file path"
        )
    if not isinstance(content, Mapping):
        raise StorageError(
            "recipe content must be a JSON object mapping, not a scalar or sequence"
        )
    data = dict(content)
    kind_hint = str(data.get("kind", "")).strip().lower()
    looks_tea = kind_hint == "tea" or (
        "leaf_g" in data and "dose_g" not in data and kind_hint not in {"hot", "flash-brew"}
    )
    if looks_tea:
        try:
            tea = TeaRecipe.from_dict(data)
        except TeaRecipeError as exc:
            raise StorageError(f"invalid tea recipe: {exc}") from exc
        return _tea_to_canonical_dict(tea), "tea"
    try:
        recipe = Recipe.from_dict(data)
        strict_validate(recipe)
    except (RecipeError, SafetyError) as exc:
        raise StorageError(f"invalid coffee recipe: {exc}") from exc
    return recipe.to_dict(), "coffee"


def _parse_recipe_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    data = _row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
    data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    if "archived_at" not in data:
        data["archived_at"] = None
    return data


def _parse_revision_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    data = _row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
    data["content"] = json.loads(data.pop("content_json"))
    data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
    return data

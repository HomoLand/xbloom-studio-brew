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

SCHEMA_VERSION = 4
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
    hook = _recipe_write_fault_hook
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


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        applied_at TEXT NOT NULL,
        checksum TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recipes (
        recipe_id TEXT PRIMARY KEY,
        kind TEXT,
        name TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source TEXT,
        provenance_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recipe_revisions (
        revision_id TEXT PRIMARY KEY,
        recipe_id TEXT NOT NULL REFERENCES recipes(recipe_id),
        revision_number INTEGER NOT NULL,
        content_json TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        parent_revision_id TEXT REFERENCES recipe_revisions(revision_id),
        created_at TEXT NOT NULL,
        provenance_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE (recipe_id, revision_number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflows (
        workflow_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        recipe_revision_id TEXT REFERENCES recipe_revisions(revision_id),
        snapshot_json TEXT,
        snapshot_sha256 TEXT,
        source TEXT,
        owner TEXT,
        machine_phase TEXT,
        recovery_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        terminal_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (workflow_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        request_id TEXT PRIMARY KEY,
        method TEXT NOT NULL,
        params_sha256 TEXT NOT NULL,
        result_json TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        workflow_id TEXT REFERENCES workflows(workflow_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS legacy_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_kind TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        record_key TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        UNIQUE (source_kind, record_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_receipts (
        name TEXT PRIMARY KEY,
        completed_at TEXT NOT NULL,
        backup_dir TEXT,
        manifest_json TEXT NOT NULL,
        stats_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS history_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT NOT NULL UNIQUE,
        event_id TEXT NOT NULL,
        recorded_at TEXT,
        outcome TEXT,
        source TEXT,
        event_kind TEXT,
        recipe_sha256 TEXT,
        recipe_name TEXT,
        recipe_path TEXT,
        machine TEXT,
        serving_kind TEXT,
        machine_program TEXT,
        note TEXT,
        related_event_id TEXT,
        remote_table_id TEXT,
        workflow_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_recipe_revisions_recipe ON recipe_revisions(recipe_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow ON workflow_events(workflow_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_legacy_imports_kind ON legacy_imports(source_kind)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_recorded ON history_events(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_outcome ON history_events(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_source ON history_events(source)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_event_id ON history_events(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_recipe_sha ON history_events(recipe_sha256)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_workflow ON history_events(workflow_id)",
    "CREATE INDEX IF NOT EXISTS idx_history_events_remote ON history_events(remote_table_id)",
)

# Incremental migrations applied when opening a database created at an older
# SCHEMA_VERSION. Each entry is (target_version, name, statements).
# Version 1 was create-only baseline (SCHEMA_STATEMENTS). Version 2 adds
# active-workflow and idempotency-status indexes used by Phase A bridge APIs.
# Version 3 adds non-destructive recipe archive + listing indexes (Phase B B8).
# Version 4 adds append-only history_events journal (Phase 0.3/0.4 history cutover).
# Do not fold v3 columns into SCHEMA_STATEMENTS CREATE: fresh DBs apply CREATE
# then run migrations once; putting archived_at in CREATE would make ALTER fail
# or require dual-path schema init. New tables may appear in both CREATE (IF NOT
# EXISTS) and the versioned migration for upgrade paths.
SCHEMA_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        2,
        "phase_a_workflow_idempotency_indexes_v2",
        (
            "CREATE INDEX IF NOT EXISTS idx_workflows_state_updated "
            "ON workflows(state, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_workflows_terminal_updated "
            "ON workflows(terminal_at, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_idempotency_status "
            "ON idempotency(status, created_at)",
        ),
    ),
    (
        3,
        "phase_b_recipe_archive_list_indexes_v3",
        (
            "ALTER TABLE recipes ADD COLUMN archived_at TEXT",
            "CREATE INDEX IF NOT EXISTS idx_recipes_updated_at "
            "ON recipes(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_kind_updated "
            "ON recipes(kind, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_archived_updated "
            "ON recipes(archived_at, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_revisions_recipe_number "
            "ON recipe_revisions(recipe_id, revision_number)",
        ),
    ),
    (
        4,
        "phase_0_history_events_journal_v4",
        (
            """
            CREATE TABLE IF NOT EXISTS history_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                event_id TEXT NOT NULL,
                recorded_at TEXT,
                outcome TEXT,
                source TEXT,
                event_kind TEXT,
                recipe_sha256 TEXT,
                recipe_name TEXT,
                recipe_path TEXT,
                machine TEXT,
                serving_kind TEXT,
                machine_program TEXT,
                note TEXT,
                related_event_id TEXT,
                remote_table_id TEXT,
                workflow_id TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_history_events_recorded "
            "ON history_events(recorded_at)",
            "CREATE INDEX IF NOT EXISTS idx_history_events_outcome "
            "ON history_events(outcome)",
            "CREATE INDEX IF NOT EXISTS idx_history_events_source "
            "ON history_events(source)",
            "CREATE INDEX IF NOT EXISTS idx_history_events_event_id "
            "ON history_events(event_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_events_recipe_sha "
            "ON history_events(recipe_sha256)",
            "CREATE INDEX IF NOT EXISTS idx_history_events_workflow "
            "ON history_events(workflow_id)",
            "CREATE INDEX IF NOT EXISTS idx_history_events_remote "
            "ON history_events(remote_table_id)",
        ),
    ),
)


class StateStore:
    """Thread-aware SQLite store scoped to one normalised state root."""

    def __init__(
        self,
        state_root: Path | str | None = None,
        *,
        db_path: Path | str | None = None,
    ) -> None:
        if db_path is not None:
            self.db_path = Path(db_path)
            self.state_root = normalize_state_root(self.db_path.parent)
        else:
            root = (
                normalize_state_root(state_root)
                if state_root is not None
                else resolve_state_dir()
            )
            self.state_root = root
            self.db_path = root / DB_FILE_NAME
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions only
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @staticmethod
    def _migration_version(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"] or 0)

    @staticmethod
    def _execute_schema_ddl(conn: sqlite3.Connection, statement: str) -> None:
        """Run one schema DDL statement with idempotent ALTER TABLE ADD COLUMN."""

        match = _ALTER_ADD_COLUMN_RE.match(statement)
        if match is not None:
            table_name, column_name = match.group(1), match.group(2)
            existing = {
                str(row[1])
                for row in conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if column_name in existing:
                return
        conn.execute(statement)

    def _bootstrap_schema_once(self) -> int:
        """Apply baseline + migrations with exclusive locks and version re-check.

        Each version still commits in its own transaction (fault rolls back only
        the in-flight step). Concurrent StateStore instances re-read version
        after BEGIN IMMEDIATE so only one applies each step; ALTER ADD COLUMN
        is also idempotent as defense in depth.
        """

        conn = self._connect()

        # Base objects + optional baseline v1 row.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            current = self._migration_version(conn)
            if current == 0:
                checksum = sha256_text(
                    "\n".join(s.strip() for s in SCHEMA_STATEMENTS)
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations
                        (version, name, applied_at, checksum)
                    VALUES (?, ?, ?, ?)
                    """,
                    (1, "baseline_v1", utc_now(), checksum),
                )
                current = self._migration_version(conn)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

        if current > SCHEMA_VERSION:
            raise StorageError(
                f"database schema version {current} is newer than "
                f"supported {SCHEMA_VERSION}"
            )

        for target_version, name, statements in SCHEMA_MIGRATIONS:
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = self._migration_version(conn)
                if current >= target_version:
                    conn.execute("COMMIT")
                    continue
                if target_version != current + 1:
                    raise StorageError(
                        f"schema migration gap: at {current}, next is {target_version}"
                    )
                checksum = sha256_text("\n".join(s.strip() for s in statements))
                for statement in statements:
                    self._execute_schema_ddl(conn, statement)
                conn.execute(
                    """
                    INSERT INTO schema_migrations
                        (version, name, applied_at, checksum)
                    VALUES (?, ?, ?, ?)
                    """,
                    (target_version, name, utc_now(), checksum),
                )
                conn.execute("COMMIT")
                current = target_version
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        return self._migration_version(conn)

    def ensure_schema(self) -> int:
        """Create/migrate schema to SCHEMA_VERSION. Returns current version.

        Existing v1 databases are upgraded in place via SCHEMA_MIGRATIONS rather
        than assuming create-only. Each version is recorded in schema_migrations.

        Safe across independent connections: exclusive BEGIN IMMEDIATE, version
        re-check, idempotent ALTER ADD COLUMN, and busy/locked retries. Does not
        use a process-global lock.
        """

        with self._init_lock:
            if self._initialized:
                row = self._connect().execute(
                    "SELECT MAX(version) AS v FROM schema_migrations"
                ).fetchone()
                return int(row["v"] or 0)

            last_error: BaseException | None = None
            for attempt in range(SCHEMA_INIT_MAX_ATTEMPTS):
                try:
                    current = self._bootstrap_schema_once()
                    self._initialized = True
                    return current
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    if not _is_sqlite_locked_error(exc):
                        raise
                except sqlite3.IntegrityError as exc:
                    # Concurrent migration insert: re-read after backoff.
                    last_error = exc
                self.close()
                delay = min(
                    SCHEMA_INIT_BASE_DELAY_S * (2 ** min(attempt, 6)),
                    0.25,
                )
                time.sleep(delay)

            raise StorageError(
                "schema initialization failed after concurrent access retries"
            ) from last_error

    def schema_version(self) -> int:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT MAX(version) AS v FROM schema_migrations"
        ).fetchone()
        return int(row["v"] or 0)

    def list_migrations(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        rows = self._connect().execute(
            "SELECT version, name, applied_at, checksum FROM schema_migrations "
            "ORDER BY version"
        ).fetchall()
        return [_row_to_dict(row) or {} for row in rows]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction; nested use reuses the outer connection."""

        self.ensure_schema()
        conn = self._connect()
        in_tx = getattr(self._local, "in_tx", False)
        if in_tx:
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.in_tx = True
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            self._local.in_tx = False

    # ------------------------------------------------------------------
    # Recipe primitives
    # ------------------------------------------------------------------

    def get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        """Return one recipe row with parsed provenance/metadata, or None."""

        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_recipe_row(row)

    def upsert_recipe(
        self,
        *,
        recipe_id: str | None = None,
        kind: str | None = None,
        name: str | None = None,
        source: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert or update a recipe; omitted fields preserve stored values.

        When ``provenance`` / ``metadata`` are omitted (``None``), existing JSON
        columns are left unchanged on update (SQL COALESCE). The return value is
        the actual stored row after the write.

        Low-level primitive: does not validate content or sanitize provenance
        (used by legacy import / bridge). Prefer
        :meth:`create_recipe_with_revision` for catalog writes.
        """

        rid = recipe_id or f"rcp_{uuid4().hex}"
        now = utc_now()
        prov_json = (
            canonical_json(dict(provenance)) if provenance is not None else None
        )
        meta_json = canonical_json(dict(metadata)) if metadata is not None else None
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_id = ?",
                (rid,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE recipes SET
                        kind = COALESCE(?, kind),
                        name = COALESCE(?, name),
                        updated_at = ?,
                        source = COALESCE(?, source),
                        provenance_json = COALESCE(?, provenance_json),
                        metadata_json = COALESCE(?, metadata_json)
                    WHERE recipe_id = ?
                    """,
                    (kind, name, now, source, prov_json, meta_json, rid),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO recipes (
                        recipe_id, kind, name, created_at, updated_at,
                        source, provenance_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        kind,
                        name,
                        now,
                        now,
                        source,
                        prov_json if prov_json is not None else canonical_json({}),
                        meta_json if meta_json is not None else canonical_json({}),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?",
                (rid,),
            ).fetchone()
        return _parse_recipe_row(row)

    def add_recipe_revision(
        self,
        recipe_id: str,
        content: Mapping[str, Any],
        *,
        revision_id: str | None = None,
        parent_revision_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Low-level revision insert without domain validation or OCC.

        Prefer :meth:`create_recipe_revision` for Web/catalog edits (validates
        content, requires expected parent, updates recipe display fields).
        """

        rev_id = revision_id or f"rev_{uuid4().hex}"
        content_json = canonical_json(dict(content))
        digest = sha256_text(content_json)
        prov = canonical_json(dict(provenance or {}))
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown recipe_id {recipe_id!r}")
            if parent_revision_id is not None:
                parent = conn.execute(
                    "SELECT recipe_id FROM recipe_revisions WHERE revision_id = ?",
                    (parent_revision_id,),
                ).fetchone()
                if parent is None:
                    raise StorageError(
                        f"unknown parent_revision_id {parent_revision_id!r}"
                    )
                if parent["recipe_id"] != recipe_id:
                    raise StorageError(
                        f"parent_revision_id {parent_revision_id!r} belongs to a "
                        f"different recipe (expected {recipe_id!r})"
                    )
            max_row = conn.execute(
                "SELECT MAX(revision_number) AS n FROM recipe_revisions "
                "WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            number = int(max_row["n"] or 0) + 1
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev_id,
                        recipe_id,
                        number,
                        content_json,
                        digest,
                        parent_revision_id,
                        now,
                        prov,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"recipe revision integrity failure: {exc}"
                ) from exc
        return {
            "revision_id": rev_id,
            "recipe_id": recipe_id,
            "revision_number": number,
            "content_sha256": digest,
            "parent_revision_id": parent_revision_id,
            "created_at": now,
            "provenance": dict(provenance or {}),
            "content": dict(content),
        }

    def get_recipe_revision(self, revision_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT r.*, recipes.kind AS recipe_kind, recipes.name AS recipe_name
            FROM recipe_revisions r
            LEFT JOIN recipes ON recipes.recipe_id = r.recipe_id
            WHERE r.revision_id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_revision_row(row)

    # ------------------------------------------------------------------
    # High-level recipe / revision API (Phase B B8)
    # ------------------------------------------------------------------

    def create_recipe_with_revision(
        self,
        content: Mapping[str, Any] | Any,
        *,
        recipe_id: str | None = None,
        name: str | None = None,
        source: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        creation_source: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create one stable recipe and its first immutable revision.

        Validates/canonicalizes content **before** opening a write transaction.
        Returns ``{"recipe": ..., "revision": ...}``.
        """

        canonical, kind = canonicalize_recipe_content(content)
        safe_prov = sanitize_recipe_provenance(
            provenance, creation_source=creation_source
        )
        if metadata is not None and not isinstance(metadata, Mapping):
            raise StorageError("metadata must be a mapping")
        meta = dict(metadata or {})
        display_name = (
            str(name) if name is not None else str(canonical.get("name") or "Unnamed")
        )
        rid = recipe_id or f"rcp_{uuid4().hex}"
        rev_id = revision_id or f"rev_{uuid4().hex}"
        content_json = canonical_json(canonical)
        digest = sha256_text(content_json)
        prov_json = canonical_json(safe_prov)
        meta_json = canonical_json(meta)
        now = utc_now()

        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_id = ?",
                (rid,),
            ).fetchone()
            if existing is not None:
                raise StorageConflictError(f"recipe_id already exists: {rid!r}")
            _maybe_recipe_write_fault("before_recipe_insert")
            conn.execute(
                """
                INSERT INTO recipes (
                    recipe_id, kind, name, created_at, updated_at,
                    source, provenance_json, metadata_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    rid,
                    kind,
                    display_name,
                    now,
                    now,
                    source,
                    prov_json,
                    meta_json,
                ),
            )
            _maybe_recipe_write_fault("before_revision_insert")
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at, provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (rev_id, rid, content_json, digest, now, prov_json),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"recipe revision integrity failure: {exc}"
                ) from exc
            recipe_row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?", (rid,)
            ).fetchone()
            rev_row = conn.execute(
                "SELECT * FROM recipe_revisions WHERE revision_id = ?", (rev_id,)
            ).fetchone()

        return {
            "recipe": _parse_recipe_row(recipe_row),
            "revision": _parse_revision_row(rev_row),
        }

    def create_recipe_revision(
        self,
        recipe_id: str,
        content: Mapping[str, Any] | Any,
        *,
        expected_parent_revision_id: str,
        name: str | None = None,
        source: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        creation_source: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a new revision with mandatory expected-parent OCC.

        Requires ``expected_parent_revision_id`` to match the current latest
        revision. Updates recipe display fields / provenance / metadata in the
        same ``BEGIN IMMEDIATE`` transaction. Raises
        :class:`StorageConflictError` on stale parent (no lost update, no
        duplicate ``revision_number``).
        """

        if not expected_parent_revision_id:
            raise StorageError("expected_parent_revision_id is required")
        canonical, kind = canonicalize_recipe_content(content)
        safe_prov = sanitize_recipe_provenance(
            provenance,
            parent_revision_id=expected_parent_revision_id,
            creation_source=creation_source,
        )
        if metadata is not None and not isinstance(metadata, Mapping):
            raise StorageError("metadata must be a mapping")
        display_name = (
            str(name) if name is not None else str(canonical.get("name") or "Unnamed")
        )
        rev_id = revision_id or f"rev_{uuid4().hex}"
        content_json = canonical_json(canonical)
        digest = sha256_text(content_json)
        prov_json = canonical_json(safe_prov)
        now = utc_now()

        with self.transaction() as conn:
            recipe_row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if recipe_row is None:
                raise StorageError(f"unknown recipe_id {recipe_id!r}")

            latest = conn.execute(
                """
                SELECT revision_id, revision_number FROM recipe_revisions
                WHERE recipe_id = ?
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                (recipe_id,),
            ).fetchone()
            if latest is None:
                raise StorageError(
                    f"recipe {recipe_id!r} has no revisions; use "
                    "create_recipe_with_revision"
                )
            if latest["revision_id"] != expected_parent_revision_id:
                raise StorageConflictError(
                    f"expected parent revision {expected_parent_revision_id!r} "
                    f"but latest is {latest['revision_id']!r}"
                )
            number = int(latest["revision_number"]) + 1

            meta_json: str | None
            if metadata is not None:
                meta_json = canonical_json(dict(metadata))
            else:
                meta_json = None

            _maybe_recipe_write_fault("before_revision_insert")
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev_id,
                        recipe_id,
                        number,
                        content_json,
                        digest,
                        expected_parent_revision_id,
                        now,
                        prov_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Concurrent winner already took this revision_number.
                raise StorageConflictError(
                    f"recipe revision conflict for {recipe_id!r}: {exc}"
                ) from exc

            _maybe_recipe_write_fault("before_recipe_update")
            conn.execute(
                """
                UPDATE recipes SET
                    kind = ?,
                    name = ?,
                    updated_at = ?,
                    source = COALESCE(?, source),
                    provenance_json = ?,
                    metadata_json = COALESCE(?, metadata_json)
                WHERE recipe_id = ?
                """,
                (
                    kind,
                    display_name,
                    now,
                    source,
                    prov_json,
                    meta_json,
                    recipe_id,
                ),
            )
            recipe_out = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()
            rev_out = conn.execute(
                "SELECT * FROM recipe_revisions WHERE revision_id = ?", (rev_id,)
            ).fetchone()

        return {
            "recipe": _parse_recipe_row(recipe_out),
            "revision": _parse_revision_row(rev_out),
        }

    def get_latest_recipe_revision(self, recipe_id: str) -> dict[str, Any] | None:
        """Return the latest revision for *recipe_id*, or None."""

        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT * FROM recipe_revisions
            WHERE recipe_id = ?
            ORDER BY revision_number DESC
            LIMIT 1
            """,
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_revision_row(row)

    def list_recipe_revisions(self, recipe_id: str) -> list[dict[str, Any]]:
        """List revisions for *recipe_id* in stable ``revision_number`` order."""

        self.ensure_schema()
        rows = self._connect().execute(
            """
            SELECT * FROM recipe_revisions
            WHERE recipe_id = ?
            ORDER BY revision_number ASC
            """,
            (recipe_id,),
        ).fetchall()
        return [_parse_revision_row(row) for row in rows]

    def list_recipes(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = DEFAULT_RECIPE_LIST_LIMIT,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List recipes with latest revision summary/content.

        Filters: optional storage *kind* (``coffee``/``tea``), case-insensitive
        name/id *query*, pagination bounds, and *include_archived*.
        """

        if not isinstance(limit, int) or not (1 <= limit <= MAX_RECIPE_LIST_LIMIT):
            raise StorageError(
                f"limit must be an integer 1..{MAX_RECIPE_LIST_LIMIT}, got {limit!r}"
            )
        if not isinstance(offset, int) or offset < 0:
            raise StorageError(f"offset must be a non-negative integer, got {offset!r}")

        self.ensure_schema()
        clauses: list[str] = []
        params: list[Any] = []
        if not include_archived:
            clauses.append("r.archived_at IS NULL")
        if kind is not None:
            clauses.append("r.kind = ?")
            params.append(kind)
        if query is not None and str(query).strip():
            like = f"%{str(query).strip()}%"
            clauses.append("(r.name LIKE ? COLLATE NOCASE OR r.recipe_id LIKE ? COLLATE NOCASE)")
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT r.*,
                   rev.revision_id AS latest_revision_id,
                   rev.revision_number AS latest_revision_number,
                   rev.content_json AS latest_content_json,
                   rev.content_sha256 AS latest_content_sha256,
                   rev.parent_revision_id AS latest_parent_revision_id,
                   rev.created_at AS latest_revision_created_at,
                   rev.provenance_json AS latest_provenance_json
            FROM recipes r
            LEFT JOIN recipe_revisions rev
              ON rev.recipe_id = r.recipe_id
             AND rev.revision_number = (
                    SELECT MAX(rr.revision_number)
                    FROM recipe_revisions rr
                    WHERE rr.recipe_id = r.recipe_id
             )
            {where}
            ORDER BY r.updated_at DESC, r.recipe_id ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = self._connect().execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = _parse_recipe_row(row)
            latest: dict[str, Any] | None = None
            if data.get("latest_revision_id"):
                latest = {
                    "revision_id": data.pop("latest_revision_id"),
                    "recipe_id": data["recipe_id"],
                    "revision_number": data.pop("latest_revision_number"),
                    "content": json.loads(data.pop("latest_content_json")),
                    "content_sha256": data.pop("latest_content_sha256"),
                    "parent_revision_id": data.pop("latest_parent_revision_id"),
                    "created_at": data.pop("latest_revision_created_at"),
                    "provenance": json.loads(
                        data.pop("latest_provenance_json") or "{}"
                    ),
                }
            else:
                for key in (
                    "latest_revision_id",
                    "latest_revision_number",
                    "latest_content_json",
                    "latest_content_sha256",
                    "latest_parent_revision_id",
                    "latest_revision_created_at",
                    "latest_provenance_json",
                ):
                    data.pop(key, None)
            data["latest_revision"] = latest
            result.append(data)
        return result

    def archive_recipe(
        self,
        recipe_id: str,
        *,
        expected_latest_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Soft-archive a recipe without deleting revisions or breaking workflows.

        When *expected_latest_revision_id* is provided it must match the current
        latest revision (guards against archiving over a newer browser edit).
        """

        return self._set_recipe_archived(
            recipe_id,
            archived=True,
            expected_latest_revision_id=expected_latest_revision_id,
        )

    def restore_recipe(
        self,
        recipe_id: str,
        *,
        expected_latest_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a previously archived recipe (revisions unchanged)."""

        return self._set_recipe_archived(
            recipe_id,
            archived=False,
            expected_latest_revision_id=expected_latest_revision_id,
        )

    def _set_recipe_archived(
        self,
        recipe_id: str,
        *,
        archived: bool,
        expected_latest_revision_id: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown recipe_id {recipe_id!r}")
            if expected_latest_revision_id is not None:
                latest = conn.execute(
                    """
                    SELECT revision_id FROM recipe_revisions
                    WHERE recipe_id = ?
                    ORDER BY revision_number DESC
                    LIMIT 1
                    """,
                    (recipe_id,),
                ).fetchone()
                latest_id = latest["revision_id"] if latest is not None else None
                if latest_id != expected_latest_revision_id:
                    raise StorageConflictError(
                        f"expected latest revision {expected_latest_revision_id!r} "
                        f"but latest is {latest_id!r}"
                    )
            archived_at = now if archived else None
            conn.execute(
                """
                UPDATE recipes SET archived_at = ?, updated_at = ?
                WHERE recipe_id = ?
                """,
                (archived_at, now, recipe_id),
            )
            out = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()
        return _parse_recipe_row(out)

    # ------------------------------------------------------------------
    # Catalog view / merge (Phase 0.3/0.4 catalog cutover)
    # ------------------------------------------------------------------

    def build_catalog_snapshot(
        self,
        *,
        include_derived: bool = True,
        include_archived: bool = False,
        schema_version: int = 1,
    ) -> dict[str, Any]:
        """Build a public catalog dict from recipes + latest revisions.

        Catalog-owned rows (metadata.catalog_entry_id / legacy mapping) keep the
        full normalized entry envelope. Web-created recipes without catalog
        metadata appear as derived public entries when *include_derived* is set.

        When ``state.db`` does not exist yet, returns an empty snapshot without
        creating the database (read path must not invent runtime files).
        """

        if not self.db_path.is_file():
            return {
                "schema_version": schema_version,
                "created_at": None,
                "updated_at": None,
                "entries": [],
                "path": str(self.db_path),
                "source": "state.db",
                "authoritative": "sqlite",
                "exists": False,
            }
        self.ensure_schema()
        rows = self.list_recipes(
            limit=MAX_RECIPE_LIST_LIMIT,
            offset=0,
            include_archived=include_archived,
        )
        # Paginate if needed (personal catalogs are small; hard-cap safety).
        if len(rows) >= MAX_RECIPE_LIST_LIMIT:
            offset = MAX_RECIPE_LIST_LIMIT
            while True:
                batch = self.list_recipes(
                    limit=MAX_RECIPE_LIST_LIMIT,
                    offset=offset,
                    include_archived=include_archived,
                )
                if not batch:
                    break
                rows.extend(batch)
                if len(batch) < MAX_RECIPE_LIST_LIMIT:
                    break
                offset += MAX_RECIPE_LIST_LIMIT

        entries: list[dict[str, Any]] = []
        updated_at: str | None = None
        created_at: str | None = None
        for row in rows:
            entry = self._catalog_entry_from_recipe_row(row, include_derived=include_derived)
            if entry is None:
                continue
            entries.append(entry)
            ru = row.get("updated_at")
            rc = row.get("created_at")
            if isinstance(ru, str) and (updated_at is None or ru > updated_at):
                updated_at = ru
            if isinstance(rc, str) and (created_at is None or rc < created_at):
                created_at = rc
        entries.sort(
            key=lambda item: (
                str(item.get("kind") or ""),
                str(item.get("name") or "").casefold(),
                str(item.get("id") or ""),
            )
        )
        return {
            "schema_version": schema_version,
            "created_at": created_at,
            "updated_at": updated_at,
            "entries": entries,
            "path": str(self.db_path),
            "source": "state.db",
            "authoritative": "sqlite",
            "exists": True,
        }

    def get_catalog_entry(
        self,
        identifier: str,
        *,
        include_derived: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve one catalog entry by public id, table_id, or unambiguous name."""

        catalog = self.build_catalog_snapshot(
            include_derived=include_derived,
            include_archived=include_archived,
        )
        entries = list(catalog.get("entries") or [])
        exact = [e for e in entries if str(e.get("id")) == identifier]
        if exact:
            return dict(exact[0])
        by_table = [
            e
            for e in entries
            if e.get("table_id") is not None and str(e.get("table_id")) == identifier
        ]
        if len(by_table) == 1:
            return dict(by_table[0])
        matches = [
            e
            for e in entries
            if identifier.casefold() in str(e.get("name", "")).casefold()
        ]
        if len(matches) == 1:
            return dict(matches[0])
        if matches:
            raise StorageError(
                f"catalog identifier {identifier!r} is ambiguous ({len(matches)} matches)"
            )
        return None

    def list_catalog_entries(
        self,
        *,
        kind: str = "all",
        origin: str | None = None,
        query: str | None = None,
        executable_only: bool = False,
        slot_compatible_only: bool = False,
        include_derived: bool = True,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List summary catalog entries with the same filters as the Skill CLI."""

        catalog = self.build_catalog_snapshot(
            include_derived=include_derived,
            include_archived=include_archived,
        )
        needle = (query or "").strip().casefold()
        result: list[dict[str, Any]] = []
        for entry in catalog.get("entries") or []:
            if kind != "all" and entry.get("kind") != kind:
                continue
            if origin and entry.get("origin") != origin:
                continue
            if executable_only and not entry.get("executable"):
                continue
            if slot_compatible_only and not entry.get("slot_compatible"):
                continue
            haystack = " ".join(
                str(entry.get(key, ""))
                for key in ("id", "name", "origin", "author", "cup_type")
            ).casefold()
            if needle and needle not in haystack:
                continue
            result.append(
                {
                    key: entry.get(key)
                    for key in (
                        "id",
                        "table_id",
                        "name",
                        "kind",
                        "machine_program",
                        "origin",
                        "author",
                        "cup_type",
                        "executable",
                        "slot_compatible",
                        "slots",
                        "warnings",
                        "validation_errors",
                        "manual_preparation",
                        "catalog_owned",
                        "derived",
                        "recipe_id",
                    )
                    if entry.get(key) not in (None, [], "")
                }
            )
        return result

    def catalog_summary(self, *, include_derived: bool = True) -> dict[str, Any]:
        """Counts for catalog status CLI / doctor."""

        catalog = self.build_catalog_snapshot(include_derived=include_derived)
        entries = list(catalog.get("entries") or [])
        return {
            "total": len(entries),
            "coffee": sum(entry.get("kind") == "coffee" for entry in entries),
            "tea": sum(entry.get("kind") == "tea" for entry in entries),
            "executable": sum(bool(entry.get("executable")) for entry in entries),
            "slot_compatible": sum(
                bool(entry.get("slot_compatible")) for entry in entries
            ),
            "updated_at": catalog.get("updated_at"),
            "path": str(self.db_path),
            "source": "state.db",
            "authoritative": "sqlite",
            "exists": bool(catalog.get("exists", self.db_path.is_file())),
        }

    def merge_catalog_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        source: str = CATALOG_SOURCE_MERGE,
        creation_source: str | None = None,
    ) -> dict[str, Any]:
        """Transactionally merge normalized catalog entries into recipes/revisions.

        Rules (BEGIN IMMEDIATE):
        - create recipe + first revision atomically for new entry ids
        - changed recipe content creates one new immutable child revision
        - metadata-only changes update recipe metadata without a fake revision
        - unchanged replay is idempotent (no duplicate revisions)
        - never deletes another writer's revisions; concurrent writers serialize
          on IMMEDIATE and parent/revision_number uniqueness
        """

        stats: dict[str, Any] = {
            "candidates": 0,
            "created": 0,
            "updated": 0,
            "metadata_only": 0,
            "unchanged": 0,
            "skipped": 0,
        }
        now = utc_now()
        # One BEGIN IMMEDIATE transaction: any failure rolls back the whole batch.
        with self.transaction() as conn:
            for raw in entries:
                stats["candidates"] += 1
                if not isinstance(raw, Mapping):
                    raise StorageError("catalog entry must be a mapping")
                if raw.get("derived") or raw.get("catalog_owned") is False:
                    stats["skipped"] += 1
                    continue
                action = self._merge_one_catalog_entry_in_tx(
                    conn,
                    dict(raw),
                    now=now,
                    source=source,
                    creation_source=creation_source,
                )
                if action == "created":
                    stats["created"] += 1
                elif action == "updated":
                    stats["updated"] += 1
                elif action == "metadata_only":
                    stats["metadata_only"] += 1
                else:
                    stats["unchanged"] += 1
        return stats

    def archive_catalog_entry(
        self,
        *,
        entry_id: str | None = None,
        table_id: int | str | None = None,
        expected_latest_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Soft-archive one local catalog-owned entry after confirmed cloud delete.

        Targets exact catalog-owned mappings only:
        - ``entry_id`` is exact id match (never substring / name fallback)
        - ``table_id`` is exact; ambiguity fails
        - derived Web recipes are never archived through this API

        Does not delete recipe_revisions or history_events.
        """

        catalog = self.build_catalog_snapshot(
            include_derived=False, include_archived=True
        )
        entries = list(catalog.get("entries") or [])
        entry: dict[str, Any] | None = None
        if entry_id is not None:
            eid = str(entry_id).strip()
            if not eid:
                raise StorageError("entry_id must be a non-empty string")
            exact = [e for e in entries if str(e.get("id")) == eid]
            if not exact:
                raise StorageError(
                    f"catalog entry {eid!r} not found for archive "
                    "(exact catalog-owned id required; no name fallback)"
                )
            entry = exact[0]
        elif table_id is not None:
            matches = [
                e
                for e in entries
                if e.get("table_id") is not None
                and str(e.get("table_id")) == str(table_id)
            ]
            if len(matches) > 1:
                raise StorageError(
                    f"catalog table_id {table_id!r} is ambiguous ({len(matches)} matches)"
                )
            if not matches:
                raise StorageError(
                    f"catalog table_id {table_id!r} not found for archive"
                )
            entry = matches[0]
        else:
            raise StorageError(
                "entry_id or table_id is required to archive catalog entry"
            )
        if entry.get("catalog_owned") is False or entry.get("derived"):
            raise StorageError(
                "archive_catalog_entry refuses derived / non-owned recipes"
            )
        recipe_id = entry.get("recipe_id") or recipe_id_for_catalog_entry_id(
            str(entry["id"])
        )
        archived = self.archive_recipe(
            str(recipe_id),
            expected_latest_revision_id=expected_latest_revision_id,
        )
        return {
            "archived": True,
            "entry_id": entry.get("id"),
            "recipe_id": archived.get("recipe_id"),
            "table_id": entry.get("table_id"),
            "archived_at": archived.get("archived_at"),
        }

    def _catalog_entry_from_recipe_row(
        self,
        row: Mapping[str, Any],
        *,
        include_derived: bool,
    ) -> dict[str, Any] | None:
        metadata = dict(row.get("metadata") or {})
        provenance = dict(row.get("provenance") or {})
        latest = row.get("latest_revision")
        recipe_body: dict[str, Any] = {}
        if isinstance(latest, Mapping) and isinstance(latest.get("content"), Mapping):
            recipe_body = dict(latest["content"])

        owned_ids = catalog_ownership_entry_ids(metadata, provenance)
        entry_id: str | None = None
        if len(owned_ids) == 1:
            entry_id = next(iter(owned_ids))
        elif metadata.get("catalog_entry_id"):
            entry_id = str(metadata.get("catalog_entry_id")).strip() or None
        elif provenance.get("catalog_entry_id"):
            entry_id = str(provenance.get("catalog_entry_id")).strip() or None
        elif provenance.get("legacy_entry_id"):
            entry_id = str(provenance.get("legacy_entry_id")).strip() or None
        # Never treat a bare legacy_ recipe_id prefix as catalog ownership.

        envelope = metadata.get("catalog_envelope")
        if entry_id and isinstance(envelope, Mapping):
            entry = normalize_catalog_envelope(envelope)
            entry["id"] = str(entry_id)
            entry["recipe"] = recipe_body
            entry["recipe_id"] = row.get("recipe_id")
            entry["catalog_owned"] = True
            entry["derived"] = False
            if row.get("archived_at"):
                entry["archived_at"] = row.get("archived_at")
            # Prefer live name/kind from recipe row when present.
            if row.get("name"):
                entry["name"] = row.get("name")
            if row.get("kind") in {"coffee", "tea"}:
                entry["kind"] = row.get("kind")
            return entry

        # Partial legacy import: ownership markers present, body in revision.
        # provenance.sources alone (without ownership markers) is not enough.
        if entry_id and (
            "executable" in metadata
            or "slot_compatible" in metadata
            or provenance.get("legacy_entry_id")
            or provenance.get("catalog_entry_id")
            or metadata.get("catalog_entry_id")
        ):
            entry = {
                "id": str(entry_id),
                "name": row.get("name") or recipe_body.get("name") or str(entry_id),
                "kind": row.get("kind") or recipe_body.get("kind") or "coffee",
                "executable": metadata.get("executable"),
                "slot_compatible": metadata.get("slot_compatible"),
                "origin": provenance.get("origin"),
                "sources": list(provenance.get("sources") or []),
                "recipe": recipe_body,
                "recipe_id": row.get("recipe_id"),
                "catalog_owned": True,
                "derived": False,
            }
            return entry

        if not include_derived:
            return None
        # Web / high-level API recipes without catalog ownership markers.
        recipe_id = str(row.get("recipe_id") or "")
        if not recipe_id:
            return None
        kind = row.get("kind") or recipe_body.get("kind") or "coffee"
        name = row.get("name") or recipe_body.get("name") or recipe_id
        executable = False
        validation_errors: list[str] = []
        if recipe_body:
            try:
                canonicalize_recipe_content(recipe_body)
                executable = True
            except StorageError as exc:
                validation_errors.append(str(exc))
        return {
            "id": recipe_id,
            "name": name,
            "kind": kind if kind in {"coffee", "tea"} else "coffee",
            "machine_program": (
                "omni-tea-brewer" if kind == "tea" else "coffee-pour-over"
            ),
            "origin": provenance.get("origin") or row.get("source") or CATALOG_SOURCE_WEB,
            "executable": executable,
            "slot_compatible": False,
            "validation_errors": validation_errors,
            "warnings": [],
            "sources": [
                {
                    "type": "web-state",
                    "recipe_id": recipe_id,
                }
            ],
            "recipe": recipe_body,
            "recipe_id": recipe_id,
            "catalog_owned": False,
            "derived": True,
            "slots": [],
        }

    @staticmethod
    def _require_catalog_merge_ownership(
        entry_id: str,
        recipe_id: str,
        metadata: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> None:
        """Refuse to take over a row not explicitly owned by this catalog entry."""

        owned = catalog_ownership_entry_ids(metadata, provenance)
        if not owned:
            raise StorageConflictError(
                f"recipe_id {recipe_id!r} exists without catalog ownership "
                f"markers; refusing merge of catalog entry {entry_id!r}"
            )
        if owned != {entry_id}:
            raise StorageConflictError(
                f"catalog entry {entry_id!r} conflicts with existing ownership "
                f"markers {sorted(owned)!r} on recipe_id {recipe_id!r}"
            )

    def _merge_one_catalog_entry_in_tx(
        self,
        conn: sqlite3.Connection,
        entry: dict[str, Any],
        *,
        now: str,
        source: str,
        creation_source: str | None,
    ) -> str:
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            raise StorageError("catalog entry missing id")
        envelope, recipe_body = split_catalog_entry(entry)
        recipe_id = recipe_id_for_catalog_entry_id(entry_id)
        content_json = canonical_json(recipe_body)
        digest = sha256_text(content_json)

        existing_row = conn.execute(
            "SELECT * FROM recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        existing_meta: dict[str, Any] = {}
        existing_prov: dict[str, Any] = {}
        if existing_row is not None:
            existing_meta = json.loads(existing_row["metadata_json"] or "{}")
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            existing_prov = json.loads(existing_row["provenance_json"] or "{}")
            if not isinstance(existing_prov, dict):
                existing_prov = {}
            self._require_catalog_merge_ownership(
                entry_id, recipe_id, existing_meta, existing_prov
            )

        existing_envelope = existing_meta.get("catalog_envelope")
        if not isinstance(existing_envelope, Mapping):
            existing_envelope = {
                k: existing_meta[k]
                for k in ("executable", "slot_compatible")
                if k in existing_meta
            } or None
        merged_envelope = merge_catalog_envelopes(existing_envelope, envelope)
        kind = merged_envelope.get("kind") or recipe_body.get("kind")
        if kind not in {"coffee", "tea"}:
            kind = "tea" if str(recipe_body.get("kind", "")).lower() == "tea" else "coffee"
        name = str(merged_envelope.get("name") or recipe_body.get("name") or entry_id)

        # Preserve unrelated Web/user metadata and provenance annotations.
        metadata = dict(existing_meta)
        metadata["catalog_entry_id"] = entry_id
        metadata["catalog_envelope"] = normalize_catalog_envelope(merged_envelope)
        metadata["executable"] = merged_envelope.get("executable")
        metadata["slot_compatible"] = merged_envelope.get("slot_compatible")

        provenance = dict(existing_prov)
        provenance["catalog_entry_id"] = entry_id
        provenance["legacy_entry_id"] = entry_id
        if "sources" in merged_envelope:
            provenance["sources"] = merged_envelope.get("sources")
        if "origin" in merged_envelope:
            provenance["origin"] = merged_envelope.get("origin")
        provenance["source"] = source
        if creation_source:
            provenance["creation_source"] = creation_source
        meta_json = canonical_json(metadata)
        prov_json = canonical_json(provenance)

        if existing_row is None:
            conn.execute(
                """
                INSERT INTO recipes (
                    recipe_id, kind, name, created_at, updated_at,
                    source, provenance_json, metadata_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    recipe_id,
                    kind,
                    name,
                    now,
                    now,
                    source,
                    prov_json,
                    meta_json,
                ),
            )
            rev_id = f"legacy_rev_{entry_id}_{digest[:16]}"
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at,
                        provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (rev_id, recipe_id, content_json, digest, now, prov_json),
                )
            except sqlite3.IntegrityError:
                # A global deterministic revision-id collision must not leave
                # the newly inserted catalog recipe without revision 1.
                rev_id = f"rev_{uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at,
                        provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (rev_id, recipe_id, content_json, digest, now, prov_json),
                )
            return "created"

        latest = conn.execute(
            """
            SELECT revision_id, revision_number, content_sha256
            FROM recipe_revisions
            WHERE recipe_id = ?
            ORDER BY revision_number DESC
            LIMIT 1
            """,
            (recipe_id,),
        ).fetchone()

        content_same = latest is not None and latest["content_sha256"] == digest
        # Compare canonical forms so byte-stable stored JSON matches rebuilds.
        existing_meta_canon = canonical_json(existing_meta)
        existing_prov_canon = canonical_json(existing_prov)
        existing_source = existing_row["source"]
        existing_name = existing_row["name"]
        existing_kind = existing_row["kind"]
        existing_archived = existing_row["archived_at"]
        # COALESCE(?, source) with non-null source always takes the merge source.
        row_unchanged = (
            content_same
            and existing_kind == kind
            and existing_name == name
            and existing_source == source
            and existing_meta_canon == meta_json
            and existing_prov_canon == prov_json
            and existing_archived is None
        )
        if row_unchanged:
            # True idempotency: no UPDATE at all (updated_at, revision count, ...).
            return "unchanged"

        # Un-archive on re-merge so cloud re-import restores visibility.
        # Content-identical restore / envelope-only edits are metadata_only.
        conn.execute(
            """
            UPDATE recipes SET
                kind = ?,
                name = ?,
                updated_at = ?,
                source = COALESCE(?, source),
                provenance_json = ?,
                metadata_json = ?,
                archived_at = NULL
            WHERE recipe_id = ?
            """,
            (kind, name, now, source, prov_json, meta_json, recipe_id),
        )

        if latest is None:
            rev_id = f"legacy_rev_{entry_id}_{digest[:16]}"
            conn.execute(
                """
                INSERT INTO recipe_revisions (
                    revision_id, recipe_id, revision_number, content_json,
                    content_sha256, parent_revision_id, created_at,
                    provenance_json
                ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                """,
                (rev_id, recipe_id, content_json, digest, now, prov_json),
            )
            return "updated"

        if content_same:
            return "metadata_only"

        number = int(latest["revision_number"]) + 1
        rev_id = f"rev_{uuid4().hex}"
        try:
            conn.execute(
                """
                INSERT INTO recipe_revisions (
                    revision_id, recipe_id, revision_number, content_json,
                    content_sha256, parent_revision_id, created_at,
                    provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rev_id,
                    recipe_id,
                    number,
                    content_json,
                    digest,
                    latest["revision_id"],
                    now,
                    prov_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Concurrent writer took this revision_number; re-check content.
            latest2 = conn.execute(
                """
                SELECT revision_id, revision_number, content_sha256
                FROM recipe_revisions
                WHERE recipe_id = ?
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                (recipe_id,),
            ).fetchone()
            if latest2 is not None and latest2["content_sha256"] == digest:
                return "unchanged"
            raise StorageConflictError(
                f"catalog merge conflict for {entry_id!r}: {exc}"
            ) from exc
        return "updated"

    @staticmethod
    def _encode_recovery_json(
        recovery: Mapping[str, Any] | _ClearRecovery | None,
        existing_json: str | None,
    ) -> str | None:
        """Map preserve / clear / replace semantics for recovery_json."""

        if recovery is CLEAR_RECOVERY:
            return None
        if recovery is None:
            return existing_json
        return canonical_json(dict(recovery))

    # ------------------------------------------------------------------
    # Workflow primitives
    # ------------------------------------------------------------------

    def create_workflow(
        self,
        *,
        workflow_id: str | None = None,
        kind: str,
        state: str = "created",
        recipe_revision_id: str | None = None,
        snapshot: Mapping[str, Any] | None = None,
        source: str | None = None,
        owner: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        wid = workflow_id or f"wf_{uuid4().hex}"
        now = utc_now()
        snap_json = canonical_json(dict(snapshot)) if snapshot is not None else None
        snap_sha = sha256_text(snap_json) if snap_json is not None else None
        recovery_json = (
            canonical_json(dict(recovery)) if recovery is not None else None
        )
        meta_json = canonical_json(dict(metadata or {}))

        def _insert(active: sqlite3.Connection) -> dict[str, Any]:
            try:
                active.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, kind, state, recipe_revision_id, snapshot_json,
                        snapshot_sha256, source, owner, machine_phase, recovery_json,
                        created_at, updated_at, terminal_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        wid,
                        kind,
                        state,
                        recipe_revision_id,
                        snap_json,
                        snap_sha,
                        source,
                        owner,
                        machine_phase,
                        recovery_json,
                        now,
                        now,
                        meta_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"workflow integrity failure: {exc}"
                ) from exc
            return {
                "workflow_id": wid,
                "kind": kind,
                "state": state,
                "recipe_revision_id": recipe_revision_id,
                "snapshot_sha256": snap_sha,
                "source": source,
                "owner": owner,
                "machine_phase": machine_phase,
                "created_at": now,
                "updated_at": now,
            }

        if conn is not None:
            return _insert(conn)
        with self.transaction() as active:
            return _insert(active)

    def create_workflow_with_event(
        self,
        *,
        workflow_id: str | None = None,
        kind: str,
        state: str = "created",
        recipe_revision_id: str | None = None,
        snapshot: Mapping[str, Any] | None = None,
        source: str | None = None,
        owner: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        event_type: str = "created",
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically create a workflow row and its initial event.

        Either both succeed or neither is visible -- no orphan active rows.
        """

        with self.transaction() as conn:
            wf = self.create_workflow(
                workflow_id=workflow_id,
                kind=kind,
                state=state,
                recipe_revision_id=recipe_revision_id,
                snapshot=snapshot,
                source=source,
                owner=owner,
                machine_phase=machine_phase,
                recovery=recovery,
                metadata=metadata,
                conn=conn,
            )
            payload = dict(event_payload or {})
            if "kind" not in payload:
                payload["kind"] = kind
            if "state" not in payload:
                payload["state"] = state
            if "snapshot_sha256" not in payload and wf.get("snapshot_sha256"):
                payload["snapshot_sha256"] = wf["snapshot_sha256"]
            event = self.append_workflow_event_in_tx(
                conn, wf["workflow_id"], event_type, payload
            )
        return {**wf, "event": event}

    def transition_workflow(
        self,
        workflow_id: str,
        *,
        state: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = None,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically update workflow row and optionally append an event."""

        with self.transaction() as conn:
            updated = self.update_workflow_in_tx(
                conn,
                workflow_id,
                state=state,
                machine_phase=machine_phase,
                recovery=recovery,
                terminal=terminal,
                metadata=metadata,
            )
            event: dict[str, Any] | None = None
            if event_type is not None:
                payload = dict(event_payload or {})
                if state is not None and "state" not in payload:
                    payload["state"] = state
                event = self.append_workflow_event_in_tx(
                    conn, workflow_id, event_type, payload
                )
        return {**updated, "event": event}

    def update_workflow(
        self,
        workflow_id: str,
        *,
        state: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = None,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            return self.update_workflow_in_tx(
                conn,
                workflow_id,
                state=state,
                machine_phase=machine_phase,
                recovery=recovery,
                terminal=terminal,
                metadata=metadata,
            )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        data = _row_to_dict(row) or {}
        if data.get("snapshot_json"):
            data["snapshot"] = json.loads(data.pop("snapshot_json"))
        else:
            data.pop("snapshot_json", None)
            data["snapshot"] = None
        if data.get("recovery_json"):
            data["recovery"] = json.loads(data.pop("recovery_json"))
        else:
            data.pop("recovery_json", None)
            data["recovery"] = None
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

    def append_workflow_event(
        self,
        workflow_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        seq: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        payload_json = canonical_json(dict(payload or {}))
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if exists is None:
                raise StorageError(f"unknown workflow_id {workflow_id!r}")
            if seq is None:
                max_row = conn.execute(
                    "SELECT MAX(seq) AS n FROM workflow_events WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                seq = int(max_row["n"] or 0) + 1
            conn.execute(
                """
                INSERT INTO workflow_events (
                    workflow_id, seq, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (workflow_id, seq, event_type, payload_json, now),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "id": int(row_id),
            "workflow_id": workflow_id,
            "seq": seq,
            "event_type": event_type,
            "payload": dict(payload or {}),
            "created_at": now,
        }

    def list_workflow_events(
        self, workflow_id: str, *, since_seq: int = 0
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        rows = self._connect().execute(
            """
            SELECT id, workflow_id, seq, event_type, payload_json, created_at
            FROM workflow_events
            WHERE workflow_id = ? AND seq > ?
            ORDER BY seq
            """,
            (workflow_id, int(since_seq)),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = _row_to_dict(row) or {}
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def put_idempotency(
        self,
        request_id: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        result: Mapping[str, Any] | None = None,
        status: str = "completed",
        expires_at: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        params_sha = content_sha256(dict(params or {}))
        now = utc_now()
        result_json = canonical_json(dict(result)) if result is not None else None
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["params_sha256"] != params_sha:
                    raise StorageError(
                        f"idempotency conflict for request_id {request_id!r}: "
                        "params hash mismatch"
                    )
                if existing["method"] != method:
                    raise StorageError(
                        f"idempotency conflict for request_id {request_id!r}: "
                        "method mismatch"
                    )
                cached = _row_to_dict(existing) or {}
                if cached.get("result_json"):
                    cached["result"] = json.loads(cached.pop("result_json"))
                else:
                    cached.pop("result_json", None)
                    cached["result"] = None
                cached["cached"] = True
                return cached
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency (
                        request_id, method, params_sha256, result_json, status,
                        created_at, expires_at, workflow_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        method,
                        params_sha,
                        result_json,
                        status,
                        now,
                        expires_at,
                        workflow_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"idempotency integrity failure: {exc}"
                ) from exc
        return {
            "request_id": request_id,
            "method": method,
            "params_sha256": params_sha,
            "result": dict(result) if result is not None else None,
            "status": status,
            "created_at": now,
            "expires_at": expires_at,
            "workflow_id": workflow_id,
            "cached": False,
        }

    def get_idempotency(self, request_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM idempotency WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return self._normalize_idempotency_row(row)

    @staticmethod
    def _normalize_idempotency_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = _row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
        if data.get("result_json"):
            data["result"] = json.loads(data.pop("result_json"))
        else:
            data.pop("result_json", None)
            data["result"] = None
        return data

    def reserve_idempotency(
        self,
        request_id: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve a request_id before a machine write.

        Returns one of:
        - ``status=pending``, ``reserved=True`` -- newly reserved; caller may write
        - ``status=completed``, ``cached=True`` -- exact duplicate; return result
        - ``status=pending``, ``cached=True``, ``recovery_required=True`` -- prior
          attempt may have written the machine; never reissue the action
        - raises ``StorageError`` on method/params/workflow conflict
        """

        if not request_id or not str(request_id).strip():
            raise StorageError("request_id is required")
        rid = str(request_id).strip()
        params_sha = content_sha256(dict(params or {}))
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if existing is not None:
                if existing["method"] != method:
                    raise StorageError(
                        f"idempotency conflict for request_id {rid!r}: method mismatch"
                    )
                if existing["params_sha256"] != params_sha:
                    raise StorageError(
                        f"idempotency conflict for request_id {rid!r}: "
                        "params hash mismatch"
                    )
                existing_wf = existing["workflow_id"]
                if (existing_wf or None) != (workflow_id or None):
                    raise StorageError(
                        f"idempotency conflict for request_id {rid!r}: "
                        "workflow_id mismatch"
                    )
                cached = self._normalize_idempotency_row(existing)
                cached["cached"] = True
                cached["reserved"] = False
                if cached["status"] == IDEM_COMPLETED:
                    return cached
                if cached["status"] == IDEM_FAILED:
                    # Failed attempts may be retried with the same identity only
                    # after an explicit new reservation -- treat as conflict-free
                    # re-reserve by updating back to pending without a machine
                    # result. Callers that completed a clear pre-BLE failure use
                    # fail_idempotency; re-reserve is allowed for those.
                    conn.execute(
                        """
                        UPDATE idempotency SET
                            status = ?, result_json = NULL, created_at = ?,
                            expires_at = COALESCE(?, expires_at)
                        WHERE request_id = ?
                        """,
                        (IDEM_PENDING, now, expires_at, rid),
                    )
                    return {
                        "request_id": rid,
                        "method": method,
                        "params_sha256": params_sha,
                        "result": None,
                        "status": IDEM_PENDING,
                        "created_at": now,
                        "expires_at": expires_at or cached.get("expires_at"),
                        "workflow_id": workflow_id,
                        "cached": False,
                        "reserved": True,
                        "rereserved": True,
                    }
                # Pending: never reissue machine action.
                cached["recovery_required"] = True
                return cached
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency (
                        request_id, method, params_sha256, result_json, status,
                        created_at, expires_at, workflow_id
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        method,
                        params_sha,
                        IDEM_PENDING,
                        now,
                        expires_at,
                        workflow_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"idempotency integrity failure: {exc}"
                ) from exc
        return {
            "request_id": rid,
            "method": method,
            "params_sha256": params_sha,
            "result": None,
            "status": IDEM_PENDING,
            "created_at": now,
            "expires_at": expires_at,
            "workflow_id": workflow_id,
            "cached": False,
            "reserved": True,
        }

    def complete_idempotency(
        self,
        request_id: str,
        result: Mapping[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Mark a reserved request completed with a durable result."""

        rid = str(request_id).strip()
        result_json = canonical_json(dict(result))
        now = utc_now()

        def _complete(active: sqlite3.Connection) -> dict[str, Any]:
            row = active.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown request_id {rid!r}")
            if row["status"] == IDEM_COMPLETED and row["result_json"] is not None:
                cached = self._normalize_idempotency_row(row)
                cached["cached"] = True
                return cached
            active.execute(
                """
                UPDATE idempotency SET status = ?, result_json = ?
                WHERE request_id = ?
                """,
                (IDEM_COMPLETED, result_json, rid),
            )
            return {
                "request_id": rid,
                "method": row["method"],
                "params_sha256": row["params_sha256"],
                "result": dict(result),
                "status": IDEM_COMPLETED,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "workflow_id": row["workflow_id"],
                "cached": False,
                "completed_at": now,
            }

        if conn is not None:
            return _complete(conn)
        with self.transaction() as active:
            return _complete(active)

    def fail_idempotency(
        self,
        request_id: str,
        error: Mapping[str, Any] | str,
        *,
        conn: sqlite3.Connection | None = None,
        keep_pending: bool = False,
    ) -> dict[str, Any]:
        """Record a failed or recovery-bound idempotency outcome.

        When ``keep_pending`` is True (uncertain machine write), status stays
        ``pending`` so a retry surfaces recovery_required rather than reissuing.
        """

        rid = str(request_id).strip()
        if isinstance(error, str):
            payload: dict[str, Any] = {"error": error}
        else:
            payload = dict(error)
        result_json = canonical_json(payload)

        def _fail(active: sqlite3.Connection) -> dict[str, Any]:
            row = active.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown request_id {rid!r}")
            status = IDEM_PENDING if keep_pending else IDEM_FAILED
            active.execute(
                """
                UPDATE idempotency SET status = ?, result_json = ?
                WHERE request_id = ?
                """,
                (status, result_json, rid),
            )
            return {
                "request_id": rid,
                "method": row["method"],
                "params_sha256": row["params_sha256"],
                "result": payload,
                "status": status,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "workflow_id": row["workflow_id"],
                "cached": False,
                "recovery_required": keep_pending,
            }

        if conn is not None:
            return _fail(conn)
        with self.transaction() as active:
            return _fail(active)

    # ------------------------------------------------------------------
    # Active / latest workflow queries
    # ------------------------------------------------------------------

    def get_active_workflow(self) -> dict[str, Any] | None:
        """Return the newest non-terminal workflow, or None."""

        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT * FROM workflows
            WHERE terminal_at IS NULL
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self.get_workflow(str(row["workflow_id"]))

    def get_latest_workflow(self) -> dict[str, Any] | None:
        """Return the most recently updated workflow (terminal or not)."""

        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT workflow_id FROM workflows
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self.get_workflow(str(row["workflow_id"]))

    def get_latest_workflow_for_kinds(
        self,
        kinds: Sequence[str],
        *,
        terminal: bool | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest workflow among a validated set of kinds.

        Ordering is deterministic: ``updated_at`` DESC, ``created_at`` DESC,
        then SQLite ``rowid`` DESC as a final insertion-identity tie-breaker.
        ``utc_now()`` is second-resolution, so two terminal commits in the same
        second would otherwise be non-deterministic without ``rowid``.

        ``terminal`` filters:
        - ``True``: only rows with ``terminal_at`` set
        - ``False``: only non-terminal rows
        - ``None``: either
        """

        if not kinds:
            raise StorageError("kinds must be a non-empty sequence")
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in kinds:
            if not isinstance(raw, str) or not raw.strip():
                raise StorageError(f"invalid workflow kind {raw!r}")
            kind = raw.strip()
            if kind not in KNOWN_WORKFLOW_KINDS:
                raise StorageError(f"unknown workflow kind {kind!r}")
            if kind not in seen:
                seen.add(kind)
                normalized.append(kind)
        placeholders = ", ".join("?" for _ in normalized)
        if terminal is True:
            terminal_clause = "AND terminal_at IS NOT NULL"
        elif terminal is False:
            terminal_clause = "AND terminal_at IS NULL"
        else:
            terminal_clause = ""
        self.ensure_schema()
        row = self._connect().execute(
            f"""
            SELECT workflow_id FROM workflows
            WHERE kind IN ({placeholders}) {terminal_clause}
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            tuple(normalized),
        ).fetchone()
        if row is None:
            return None
        return self.get_workflow(str(row["workflow_id"]))

    def list_active_workflows(self) -> list[dict[str, Any]]:
        """All non-terminal workflows, newest first."""

        self.ensure_schema()
        rows = self._connect().execute(
            """
            SELECT workflow_id FROM workflows
            WHERE terminal_at IS NULL
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            wf = self.get_workflow(str(row["workflow_id"]))
            if wf is not None:
                result.append(wf)
        return result

    def workflow_summary(self, workflow_id: str | None = None) -> dict[str, Any] | None:
        """Public durable summary for status() / clients."""

        wf = (
            self.get_workflow(workflow_id)
            if workflow_id is not None
            else self.get_active_workflow() or self.get_latest_workflow()
        )
        if wf is None:
            return None
        return {
            "workflow_id": wf["workflow_id"],
            "kind": wf.get("kind"),
            "state": wf.get("state"),
            "source": wf.get("source"),
            "owner": wf.get("owner"),
            "snapshot_sha256": wf.get("snapshot_sha256"),
            "recipe_revision_id": wf.get("recipe_revision_id"),
            "machine_phase": wf.get("machine_phase"),
            "recovery": wf.get("recovery"),
            "created_at": wf.get("created_at"),
            "updated_at": wf.get("updated_at"),
            "terminal_at": wf.get("terminal_at"),
            "metadata": wf.get("metadata") or {},
        }

    def list_workflow_events_page(
        self,
        workflow_id: str,
        *,
        since_seq: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Durable event cursor page with explicit gap contract.

        Durable rows are dense (seq increases by 1). ``gap_detected`` is True
        only when the caller skips past known history (since > max durable seq
        for a known workflow) or the workflow is unknown. Artificial gaps are
        never introduced by append.
        """

        self.ensure_schema()
        exists = self._connect().execute(
            "SELECT 1 FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if exists is None:
            return {
                "workflow_id": workflow_id,
                "events": [],
                "next_since": int(since_seq),
                "gap_detected": True,
                "gap_reason": "unknown_workflow",
                "max_seq": 0,
            }
        max_row = self._connect().execute(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM workflow_events WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        max_seq = int(max_row["n"] or 0)
        since = max(0, int(since_seq))
        if since > max_seq:
            return {
                "workflow_id": workflow_id,
                "events": [],
                "next_since": max_seq,
                "gap_detected": True,
                "gap_reason": "since_beyond_history",
                "max_seq": max_seq,
            }
        events = self.list_workflow_events(workflow_id, since_seq=since)
        if limit is not None and limit >= 0:
            events = events[: int(limit)]
        next_since = int(events[-1]["seq"]) if events else since
        # Dense sequence: if first returned seq is not since+1 when events exist
        # and since < max, that would be a real gap -- should not happen.
        gap_detected = False
        gap_reason: str | None = None
        if events and since > 0:
            expected = since + 1
            if int(events[0]["seq"]) != expected:
                gap_detected = True
                gap_reason = "sequence_hole"
        return {
            "workflow_id": workflow_id,
            "events": events,
            "next_since": next_since if events else since,
            "gap_detected": gap_detected,
            "gap_reason": gap_reason,
            "max_seq": max_seq,
        }

    def commit_workflow_terminal(
        self,
        workflow_id: str,
        *,
        state: str,
        event_type: str = "terminal",
        event_payload: Mapping[str, Any] | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = CLEAR_RECOVERY,
        machine_phase: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically finalize workflow, workflow event, history, and idempotency.

        One ``BEGIN IMMEDIATE`` transaction covers:
        - workflow terminal row update
        - final workflow_events row
        - exactly one final history_events row (dedupe ``workflow:<id>:terminal``)
        - optional idempotency completion

        Any history insert fault rolls the whole commit back so callers must not
        claim BLE release succeeded. Terminal re-entry is idempotent for the
        history row (same dedupe key) while preserving first ``terminal_at``.

        Terminal commits default to clearing ``recovery_json`` (``CLEAR_RECOVERY``).
        Pass ``recovery=None`` to preserve an existing recovery payload, or a
        mapping to store a final recovery note.
        """

        now = utc_now()
        payload = dict(event_payload or {})
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown workflow_id {workflow_id!r}")

            # Terminal re-entry: preserve first durable terminal state/event/
            # history. Identical state returns without new workflow_events;
            # conflicting state must fail rather than rewrite the workflow
            # while leaving the first history row in place.
            if row["terminal_at"] is not None:
                if str(row["state"]) != str(state):
                    raise StorageConflictError(
                        f"workflow {workflow_id!r} already terminal in state "
                        f"{row['state']!r}; cannot re-enter with {state!r}"
                    )
                hist_key = workflow_terminal_history_dedupe_key(workflow_id)
                hist_row = conn.execute(
                    "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
                    (hist_key,),
                ).fetchone()
                history_event = (
                    self._history_row_to_public(hist_row)
                    if hist_row is not None
                    else None
                )
                # If history is missing (pre-cutover terminal), append once
                # under the terminal dedupe key without rewriting workflow.
                if history_event is None:
                    row_dict = _row_to_dict(row) or {}
                    history_payload = history_event_from_workflow_terminal(
                        row_dict,
                        state=str(row["state"]),
                        event_payload=payload,
                        terminal_at=str(row["terminal_at"]),
                    )
                    history_event, _ = self._append_history_event_in_tx(
                        conn,
                        history_payload,
                        dedupe_key=hist_key,
                        created_at=now,
                    )
                last_ev = conn.execute(
                    """
                    SELECT id, seq, event_type, payload_json, created_at
                    FROM workflow_events
                    WHERE workflow_id = ?
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (workflow_id,),
                ).fetchone()
                event_out: dict[str, Any]
                if last_ev is not None:
                    try:
                        last_payload = json.loads(last_ev["payload_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        last_payload = {}
                    event_out = {
                        "id": int(last_ev["id"]),
                        "workflow_id": workflow_id,
                        "seq": int(last_ev["seq"]),
                        "event_type": last_ev["event_type"],
                        "payload": last_payload if isinstance(last_payload, dict) else {},
                        "created_at": last_ev["created_at"],
                    }
                else:
                    event_out = {
                        "id": None,
                        "workflow_id": workflow_id,
                        "seq": 0,
                        "event_type": event_type,
                        "payload": payload,
                        "created_at": row["terminal_at"],
                    }
                idem: dict[str, Any] | None = None
                if request_id is not None and idempotency_result is not None:
                    idem = self.complete_idempotency(
                        request_id, idempotency_result, conn=conn
                    )
                elif request_id is not None:
                    idem = self.complete_idempotency(
                        request_id,
                        {
                            "status": state,
                            "workflow_id": workflow_id,
                            "terminal": True,
                            **payload,
                        },
                        conn=conn,
                    )
                return {
                    "workflow_id": workflow_id,
                    "state": str(row["state"]),
                    "terminal_at": row["terminal_at"],
                    "updated_at": row["updated_at"],
                    "event": event_out,
                    "history_event": history_event,
                    "idempotency": idem,
                    "reentered": True,
                }

            new_phase = (
                machine_phase if machine_phase is not None else row["machine_phase"]
            )
            new_recovery = self._encode_recovery_json(
                recovery, row["recovery_json"]
            )
            new_meta = (
                canonical_json(dict(metadata))
                if metadata is not None
                else row["metadata_json"]
            )
            terminal_at = now
            conn.execute(
                """
                UPDATE workflows SET
                    state = ?, machine_phase = ?, recovery_json = ?,
                    updated_at = ?, terminal_at = ?, metadata_json = ?
                WHERE workflow_id = ?
                """,
                (
                    state,
                    new_phase,
                    new_recovery,
                    now,
                    terminal_at,
                    new_meta,
                    workflow_id,
                ),
            )
            max_row = conn.execute(
                "SELECT MAX(seq) AS n FROM workflow_events WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            seq = int(max_row["n"] or 0) + 1
            payload_json = canonical_json(payload)
            conn.execute(
                """
                INSERT INTO workflow_events (
                    workflow_id, seq, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (workflow_id, seq, event_type, payload_json, now),
            )
            event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            # Build history from the immutable snapshot row + terminal payload.
            row_dict = _row_to_dict(row) or {}
            if metadata is not None:
                row_dict["metadata"] = dict(metadata)
            history_payload = history_event_from_workflow_terminal(
                row_dict,
                state=state,
                event_payload=payload,
                terminal_at=terminal_at,
            )
            history_event, _ = self._append_history_event_in_tx(
                conn,
                history_payload,
                dedupe_key=workflow_terminal_history_dedupe_key(workflow_id),
                created_at=now,
            )
            idem = None
            if request_id is not None and idempotency_result is not None:
                idem = self.complete_idempotency(
                    request_id, idempotency_result, conn=conn
                )
            elif request_id is not None:
                # Complete with terminal payload when no explicit result given.
                idem = self.complete_idempotency(
                    request_id,
                    {
                        "status": state,
                        "workflow_id": workflow_id,
                        "terminal": True,
                        **payload,
                    },
                    conn=conn,
                )
        return {
            "workflow_id": workflow_id,
            "state": state,
            "terminal_at": terminal_at,
            "updated_at": now,
            "event": {
                "id": event_id,
                "workflow_id": workflow_id,
                "seq": seq,
                "event_type": event_type,
                "payload": payload,
                "created_at": now,
            },
            "history_event": history_event,
            "idempotency": idem,
        }

    def append_workflow_event_in_tx(
        self,
        conn: sqlite3.Connection,
        workflow_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an event using an existing transaction connection."""

        now = utc_now()
        payload_json = canonical_json(dict(payload or {}))
        exists = conn.execute(
            "SELECT 1 FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if exists is None:
            raise StorageError(f"unknown workflow_id {workflow_id!r}")
        max_row = conn.execute(
            "SELECT MAX(seq) AS n FROM workflow_events WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        seq = int(max_row["n"] or 0) + 1
        conn.execute(
            """
            INSERT INTO workflow_events (
                workflow_id, seq, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, seq, event_type, payload_json, now),
        )
        row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        return {
            "id": row_id,
            "workflow_id": workflow_id,
            "seq": seq,
            "event_type": event_type,
            "payload": dict(payload or {}),
            "created_at": now,
        }

    def update_workflow_in_tx(
        self,
        conn: sqlite3.Connection,
        workflow_id: str,
        *,
        state: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = None,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update workflow row inside an existing transaction."""

        now = utc_now()
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"unknown workflow_id {workflow_id!r}")
        new_state = state if state is not None else row["state"]
        new_phase = (
            machine_phase if machine_phase is not None else row["machine_phase"]
        )
        new_recovery = self._encode_recovery_json(recovery, row["recovery_json"])
        new_meta = (
            canonical_json(dict(metadata))
            if metadata is not None
            else row["metadata_json"]
        )
        terminal_at = now if terminal else row["terminal_at"]
        conn.execute(
            """
            UPDATE workflows SET
                state = ?, machine_phase = ?, recovery_json = ?,
                updated_at = ?, terminal_at = ?, metadata_json = ?
            WHERE workflow_id = ?
            """,
            (
                new_state,
                new_phase,
                new_recovery,
                now,
                terminal_at,
                new_meta,
                workflow_id,
            ),
        )
        return {
            "workflow_id": workflow_id,
            "state": new_state,
            "machine_phase": new_phase,
            "updated_at": now,
            "terminal_at": terminal_at,
        }

    # ------------------------------------------------------------------
    # History journal (append-only; authoritative runtime store)
    # ------------------------------------------------------------------

    def _prepare_history_payload(
        self,
        event: Mapping[str, Any],
        *,
        require_outcome: bool = True,
    ) -> dict[str, Any]:
        """Validate and normalise a public history event payload."""

        payload = public_history_event(event)
        if "event_id" not in payload or not str(payload.get("event_id") or "").strip():
            payload["event_id"] = new_history_event_id()
        else:
            payload["event_id"] = str(payload["event_id"]).strip()
        if "recorded_at" not in payload or not payload.get("recorded_at"):
            payload["recorded_at"] = utc_now()
        if "schema_version" not in payload:
            payload["schema_version"] = 1
        outcome = str(payload.get("outcome") or "").strip()
        if require_outcome and outcome not in HISTORY_VALID_OUTCOMES:
            raise StorageError(
                f"history outcome must be one of {sorted(HISTORY_VALID_OUTCOMES)}; "
                f"got {outcome!r}"
            )
        if outcome:
            payload["outcome"] = outcome
        source = str(payload.get("source") or HISTORY_SOURCE_LOCAL).strip() or (
            HISTORY_SOURCE_LOCAL
        )
        payload["source"] = source
        # Bound free-text note if present.
        if "note" in payload and payload["note"] is not None:
            cleaned = _history_clean_text(
                payload["note"], field="note", max_chars=MAX_HISTORY_NOTE_CHARS
            )
            if cleaned is None:
                payload.pop("note", None)
            else:
                payload["note"] = cleaned
        return payload

    def _history_row_to_public(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            payload_json = row["payload_json"]
        else:
            payload_json = row.get("payload_json")
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError(f"corrupt history payload_json: {exc}") from exc
        if not isinstance(payload, dict):
            raise StorageError("history payload_json must be a JSON object")
        return public_history_event(payload)

    def _history_payloads_conflict(
        self,
        existing: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        original_event: Mapping[str, Any],
    ) -> bool:
        """True when a retry carries a materially different public payload.

        Auto-filled fields (``recorded_at``, ``schema_version``) are aligned from
        the existing row when the caller did not supply them, so pure retries
        remain idempotent.
        """

        left = public_history_event(existing)
        right = public_history_event(candidate)
        # Align store-assigned fields the caller did not supply so pure retries
        # and concurrent same-key inserts (e.g. app remote) stay idempotent.
        if not str(original_event.get("event_id") or "").strip():
            right["event_id"] = left.get("event_id")
        if not original_event.get("recorded_at"):
            right["recorded_at"] = left.get("recorded_at")
        if "schema_version" not in original_event:
            right["schema_version"] = left.get("schema_version", 1)
        return canonical_json(left) != canonical_json(right)

    def _assert_history_compatible(
        self,
        existing_row: sqlite3.Row | Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        original_event: Mapping[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        existing_public = self._history_row_to_public(existing_row)
        if self._history_payloads_conflict(
            existing_public, candidate, original_event=original_event
        ):
            raise StorageConflictError(
                f"history dedupe conflict for {dedupe_key!r}: "
                "retry payload differs from the durable event"
            )
        return existing_public

    def _append_history_event_in_tx(
        self,
        conn: sqlite3.Connection,
        event: Mapping[str, Any],
        *,
        dedupe_key: str | None = None,
        created_at: str | None = None,
        require_outcome: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Insert one history row inside an open transaction.

        Returns ``(public_event, created)``. Identical retries are idempotent;
        a materially different public payload for the same dedupe key raises
        :class:`StorageConflictError`.
        """

        original_event = dict(event)
        payload = self._prepare_history_payload(
            event, require_outcome=require_outcome
        )
        key = dedupe_key or history_event_dedupe_key(str(payload["event_id"]))
        now = created_at or utc_now()
        existing = conn.execute(
            "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            public = self._assert_history_compatible(
                existing,
                payload,
                original_event=original_event,
                dedupe_key=key,
            )
            return public, False

        remote_id = payload.get("remote_table_id")
        remote_s = None if remote_id is None else str(remote_id)
        try:
            conn.execute(
                """
                INSERT INTO history_events (
                    dedupe_key, event_id, recorded_at, outcome, source, event_kind,
                    recipe_sha256, recipe_name, recipe_path, machine, serving_kind,
                    machine_program, note, related_event_id, remote_table_id,
                    workflow_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    str(payload["event_id"]),
                    payload.get("recorded_at"),
                    payload.get("outcome"),
                    payload.get("source"),
                    payload.get("event_kind"),
                    payload.get("recipe_sha256"),
                    payload.get("recipe_name"),
                    payload.get("recipe_path"),
                    payload.get("machine"),
                    payload.get("serving_kind"),
                    payload.get("machine_program"),
                    payload.get("note"),
                    payload.get("related_event_id"),
                    remote_s,
                    payload.get("workflow_id"),
                    canonical_json(payload),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Concurrent insert of the same dedupe key: return winner if compatible.
            raced = conn.execute(
                "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
                (key,),
            ).fetchone()
            if raced is not None:
                public = self._assert_history_compatible(
                    raced,
                    payload,
                    original_event=original_event,
                    dedupe_key=key,
                )
                return public, False
            raise StorageError(f"history insert integrity failure: {exc}") from exc
        return public_history_event(payload), True

    def append_history_event(
        self,
        event: Mapping[str, Any],
        *,
        dedupe_key: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Append one public history event. Idempotent on ``dedupe_key`` / event_id."""

        if conn is not None:
            public, _ = self._append_history_event_in_tx(
                conn, event, dedupe_key=dedupe_key
            )
            return public
        with self.transaction() as active:
            public, _ = self._append_history_event_in_tx(
                active, event, dedupe_key=dedupe_key
            )
            return public

    def load_history_events(self) -> list[dict[str, Any]]:
        """Return all history events in append order (oldest first)."""

        self.ensure_schema()
        rows = self._connect().execute(
            "SELECT payload_json FROM history_events ORDER BY id ASC"
        ).fetchall()
        return [self._history_row_to_public(row) for row in rows]

    def list_history_events(
        self,
        *,
        limit: int = DEFAULT_HISTORY_LIST_LIMIT,
        source: str | None = None,
        outcome: str | None = None,
        query: str | None = None,
        recipe_sha256: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filter history newest-first with the public list contract.

        Free-text ``query`` is applied as SQL ``LIKE`` over public text columns
        across the full matching journal (no newest-window cutoff), then limited.
        """

        if not 1 <= int(limit) <= MAX_HISTORY_LIST_LIMIT:
            raise StorageError(
                f"history list limit must be 1-{MAX_HISTORY_LIST_LIMIT}"
            )
        needle = (query or "").strip()
        wanted_source = (source or "").strip() or None
        wanted_outcome = (outcome or "").strip() or None
        wanted_sha = (recipe_sha256 or "").strip() or None

        clauses: list[str] = []
        params: list[Any] = []
        if wanted_source is not None:
            clauses.append("source = ?")
            params.append(wanted_source)
        if wanted_outcome is not None:
            clauses.append("outcome = ?")
            params.append(wanted_outcome)
        if wanted_sha is not None:
            clauses.append("recipe_sha256 = ?")
            params.append(wanted_sha)
        if needle:
            # Escape LIKE metacharacters so the needle is a literal substring.
            like_raw = (
                needle.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like = f"%{like_raw}%"
            text_cols = (
                "event_id",
                "recipe_name",
                "recipe_path",
                "machine",
                "note",
                "serving_kind",
                "machine_program",
                "outcome",
                "source",
            )
            like_parts = [
                f"LOWER(IFNULL({col}, '')) LIKE LOWER(?) ESCAPE '\\'"
                for col in text_cols
            ]
            clauses.append(f"({' OR '.join(like_parts)})")
            params.extend([like] * len(text_cols))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        self.ensure_schema()
        rows = self._connect().execute(
            f"""
            SELECT payload_json FROM history_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        return [self._history_row_to_public(row) for row in rows]

    def history_summary(self) -> dict[str, Any]:
        """Aggregate counts; path/source declare SQLite state.db as authoritative."""

        events = self.load_history_events()
        by_outcome: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for event in events:
            outcome = str(event.get("outcome") or "unknown")
            source = str(event.get("source") or "unknown")
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
        return {
            "path": str(self.db_path),
            "db_path": str(self.db_path),
            "state_root": str(self.state_root),
            "source": "state.db",
            "authoritative": "sqlite",
            "exists": self.db_path.is_file(),
            "total": len(events),
            "by_outcome": by_outcome,
            "by_source": by_source,
            "latest_recorded_at": events[-1].get("recorded_at") if events else None,
        }

    def add_history_note(self, event_id: str, note: str) -> dict[str, Any]:
        """Append a linked tasting note without rewriting earlier rows."""

        cleaned = _history_clean_text(
            note, field="note", max_chars=MAX_HISTORY_NOTE_CHARS
        )
        if not cleaned:
            raise StorageError("note must be non-empty")
        target_id = str(event_id or "").strip()
        if not target_id:
            raise StorageError("event_id is required")
        matches = [
            event
            for event in self.load_history_events()
            if event.get("event_id") == target_id
        ]
        if not matches:
            raise StorageError(f"history event {target_id!r} was not found")
        target = matches[-1]
        return self.append_history_event(
            {
                "outcome": target.get("outcome") or "imported",
                "source": HISTORY_SOURCE_LOCAL,
                "event_kind": "note",
                "related_event_id": target_id,
                "recipe_name": target.get("recipe_name"),
                "recipe_path": target.get("recipe_path"),
                "recipe_sha256": target.get("recipe_sha256"),
                "machine": target.get("machine"),
                "serving_kind": target.get("serving_kind"),
                "machine_program": target.get("machine_program"),
                "note": cleaned,
            }
        )

    def import_app_history_records(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Import App brew-record dicts; skip known remote_table_id values.

        Concurrent losers that race on the same remote dedupe key count as
        ``skipped_existing`` (one durable row only), not as ``imported``.
        """

        existing = self.load_history_events()
        known_remote_ids = {
            str(event.get("remote_table_id"))
            for event in existing
            if event.get("source") == HISTORY_SOURCE_APP
            and event.get("remote_table_id") is not None
        }
        imported = 0
        skipped = 0
        written: list[dict[str, Any]] = []
        with self.transaction() as conn:
            for raw in records:
                remote_id = raw.get("remote_table_id")
                if remote_id is not None and str(remote_id) in known_remote_ids:
                    skipped += 1
                    continue
                event = {
                    "event_kind": "app_brew_record",
                    "outcome": "imported",
                    "source": HISTORY_SOURCE_APP,
                    "region": region,
                    "remote_table_id": remote_id,
                    "recipe_name": raw.get("recipe_name"),
                    "serving_kind": raw.get("serving_kind"),
                    "machine_program": raw.get("machine_program"),
                    "cup_type": raw.get("cup_type"),
                    "dose_g": raw.get("dose_g"),
                    "brew_time_s": raw.get("brew_time_s"),
                    "create_time_stamp": raw.get("create_time_stamp"),
                    # Leave recorded_at unset when absent so prepare() fills it
                    # and concurrent same-remote races can align for comparison.
                    "recorded_at": raw.get("recorded_at"),
                    "has_line_chart": bool(raw.get("has_line_chart")),
                    "is_pod": raw.get("is_pod"),
                    "machine_id": raw.get("machine_id"),
                    "member_used_recipes_id": raw.get("member_used_recipes_id"),
                    "group_name": raw.get("group_name"),
                    "recipe_sha256": raw.get("recipe_sha256"),
                }
                event = {
                    key: value
                    for key, value in event.items()
                    if value not in (None, "", [], {})
                }
                # Prefer stable remote dedupe when a table id is present.
                dedupe: str | None = None
                if remote_id is not None:
                    dedupe = f"app:remote:{remote_id}"
                    # Re-check under the write transaction so concurrent losers
                    # observe the winner before counting as imported.
                    present = conn.execute(
                        "SELECT 1 FROM history_events WHERE dedupe_key = ?",
                        (dedupe,),
                    ).fetchone()
                    if present is not None:
                        known_remote_ids.add(str(remote_id))
                        skipped += 1
                        continue
                written_event, created = self._append_history_event_in_tx(
                    conn, event, dedupe_key=dedupe
                )
                if not created:
                    # Race: peer already inserted the same remote key.
                    if remote_id is not None:
                        known_remote_ids.add(str(remote_id))
                    skipped += 1
                    continue
                written.append(written_event)
                if remote_id is not None:
                    known_remote_ids.add(str(remote_id))
                imported += 1
        return {
            "imported": imported,
            "skipped_existing": skipped,
            "written_event_ids": [item["event_id"] for item in written],
        }

    def count_history_events(self) -> int:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT COUNT(*) AS n FROM history_events"
        ).fetchone()
        return int(row["n"])

    def get_history_event_by_dedupe_key(
        self, dedupe_key: str
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if row is None:
            return None
        return self._history_row_to_public(row)

    # ------------------------------------------------------------------
    # Integrity and backup
    # ------------------------------------------------------------------

    def integrity_check(self) -> dict[str, Any]:
        self.ensure_schema()
        conn = self._connect()
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        messages = [str(row[0]) for row in rows]
        ok = messages == ["ok"]
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        fk_issues = [
            {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]}
            for r in foreign
        ]
        return {
            "ok": ok and not fk_issues,
            "integrity_check": messages,
            "foreign_key_issues": fk_issues,
            "schema_version": self.schema_version(),
            "db_path": str(self.db_path),
        }

    def backup(self, destination: Path | str | None = None) -> Path:
        """Online backup via SQLite backup API. Returns destination path."""

        self.ensure_schema()
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            dest_dir = self.state_root / DEFAULT_BACKUP_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"state-{stamp}.db"
            # Avoid collisions if two backups start in the same microsecond.
            if dest.exists():
                dest = dest_dir / f"state-{stamp}-{uuid4().hex[:8]}.db"
        else:
            dest = Path(destination)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise StorageError(f"backup destination already exists: {dest}")
        source = self._connect()
        dest_conn = sqlite3.connect(str(dest))
        try:
            source.backup(dest_conn)
        finally:
            dest_conn.close()
        return dest

    def journal_mode(self) -> str:
        self.ensure_schema()
        row = self._connect().execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    # ------------------------------------------------------------------
    # Legacy import helpers (used by migration)
    # ------------------------------------------------------------------

    def _insert_legacy_import(
        self,
        conn: sqlite3.Connection,
        *,
        source_kind: str,
        source_path: str,
        source_sha256: str,
        record_key: str,
        payload: Any,
        imported_at: str,
    ) -> bool:
        """Insert one legacy record. Returns True if newly inserted."""

        try:
            conn.execute(
                """
                INSERT INTO legacy_imports (
                    source_kind, source_path, source_sha256, record_key,
                    payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_kind,
                    source_path,
                    source_sha256,
                    record_key,
                    canonical_json(payload),
                    imported_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def migration_completed(self, name: str = LEGACY_MIGRATION_NAME) -> bool:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT 1 FROM migration_receipts WHERE name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def get_migration_receipt(
        self, name: str = LEGACY_MIGRATION_NAME
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM migration_receipts WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        data = _row_to_dict(row) or {}
        data["manifest"] = json.loads(data.pop("manifest_json") or "{}")
        data["stats"] = json.loads(data.pop("stats_json") or "{}")
        return data

    def count_legacy_imports(self, source_kind: str | None = None) -> int:
        self.ensure_schema()
        if source_kind is None:
            row = self._connect().execute(
                "SELECT COUNT(*) AS n FROM legacy_imports"
            ).fetchone()
        else:
            row = self._connect().execute(
                "SELECT COUNT(*) AS n FROM legacy_imports WHERE source_kind = ?",
                (source_kind,),
            ).fetchone()
        return int(row["n"])


def _maybe_fault(stage: str) -> None:
    hook = _migration_fault_hook
    if hook is not None:
        hook(stage)


def create_legacy_backup(
    state_root: Path,
    *,
    backup_root: Path | None = None,
) -> dict[str, Any]:
    """Copy present legacy files byte-identically into a timestamped backup.

    Originals are never modified. Returns the manifest describing the backup.
    """

    root = normalize_state_root(state_root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = (
        normalize_state_root(backup_root)
        if backup_root is not None
        else root / DEFAULT_BACKUP_DIRNAME
    )
    backup_dir = base / f"legacy-pre-migration-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    files: list[dict[str, Any]] = []
    for kind, rel in LEGACY_SOURCES:
        source = root / rel
        if not source.is_file():
            continue
        data = source.read_bytes()
        digest = sha256_bytes(data)
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if sha256_file(dest) != digest:
            raise StorageError(f"backup integrity failure for {rel}")
        files.append(
            {
                "kind": kind,
                "relative_path": rel.as_posix(),
                "sha256": digest,
                "size": len(data),
            }
        )

    manifest = {
        "created_at": utc_now(),
        "state_root": str(root),
        "backup_dir": str(backup_dir),
        "files": files,
        "file_count": len(files),
    }
    manifest["manifest_sha256"] = content_sha256(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    )
    manifest_path = backup_dir / "MANIFEST.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _import_catalog(
    store: StateStore,
    conn: sqlite3.Connection,
    path: Path,
    file_sha: str,
    imported_at: str,
) -> dict[str, int]:
    raw_text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise StorageError(f"malformed catalog at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StorageError(f"catalog at {path} must be an object")
    entries = data.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise StorageError(f"catalog entries at {path} must be a list")

    stats = {
        "entries": 0,
        "recipes": 0,
        "revisions": 0,
        "skipped": 0,
        "metadata_only": 0,
        "unchanged": 0,
    }
    # Whole-file provenance record (lossless envelope).
    store._insert_legacy_import(
        conn,
        source_kind="catalog_file",
        source_path=str(path),
        source_sha256=file_sha,
        record_key=f"file:{file_sha}",
        payload={
            "schema_version": data.get("schema_version"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "entry_count": len(entries),
            "raw": data,
        },
        imported_at=imported_at,
    )

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise StorageError(f"catalog entry {index} is not an object")
        entry_id = str(entry.get("id") or f"index:{index}")
        if "id" not in entry or not str(entry.get("id") or "").strip():
            entry = {**entry, "id": entry_id}
        record_key = f"entry:{entry_id}:{content_sha256(entry)}"
        inserted = store._insert_legacy_import(
            conn,
            source_kind="catalog",
            source_path=str(path),
            source_sha256=file_sha,
            record_key=record_key,
            payload=entry,
            imported_at=imported_at,
        )
        if not inserted:
            stats["skipped"] += 1
            # Still repair recipe/revision state on force re-run / partial import.
        else:
            stats["entries"] += 1
        action = store._merge_one_catalog_entry_in_tx(
            conn,
            dict(entry),
            now=imported_at,
            source=CATALOG_SOURCE_LEGACY,
            creation_source="legacy_import",
        )
        if action == "created":
            stats["recipes"] += 1
            stats["revisions"] += 1
        elif action == "updated":
            stats["revisions"] += 1
        elif action == "metadata_only":
            stats["metadata_only"] += 1
        else:
            stats["unchanged"] += 1
    return stats


def _backfill_catalog_from_legacy_imports(
    store: StateStore,
    conn: sqlite3.Connection,
    *,
    imported_at: str,
) -> dict[str, Any]:
    """Populate/repair recipes from already-imported catalog rows.

    Does not read or modify original catalog.json. Replays each
    ``legacy_imports`` catalog entry through the same merge rules used at
    runtime so partial v3-era recipe rows gain full catalog envelopes.
    """

    stats: dict[str, Any] = {
        "source_rows": 0,
        "created": 0,
        "updated": 0,
        "metadata_only": 0,
        "unchanged": 0,
        "errors": 0,
        "source": "legacy_imports",
    }
    rows = conn.execute(
        """
        SELECT record_key, payload_json
        FROM legacy_imports
        WHERE source_kind = 'catalog'
        ORDER BY id ASC
        """
    ).fetchall()
    for row in rows:
        stats["source_rows"] += 1
        record_key = str(row["record_key"])
        try:
            entry = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError(
                f"corrupt legacy_imports catalog payload for {record_key!r}: {exc}"
            ) from exc
        if not isinstance(entry, dict):
            raise StorageError(
                f"legacy_imports catalog payload for {record_key!r} is not an object"
            )
        if "id" not in entry or not str(entry.get("id") or "").strip():
            # record_key form entry:{id}:{hash}
            parts = record_key.split(":", 2)
            if len(parts) >= 2 and parts[0] == "entry":
                entry = {**entry, "id": parts[1]}
            else:
                entry = {**entry, "id": f"legacy:{record_key}"}
        try:
            action = store._merge_one_catalog_entry_in_tx(
                conn,
                dict(entry),
                now=imported_at,
                source=CATALOG_SOURCE_LEGACY,
                creation_source="catalog_cutover_backfill",
            )
        except StorageError as exc:
            stats["errors"] += 1
            raise StorageError(
                f"catalog cutover failed for {record_key!r}: {exc}"
            ) from exc
        if action == "created":
            stats["created"] += 1
        elif action == "updated":
            stats["updated"] += 1
        elif action == "metadata_only":
            stats["metadata_only"] += 1
        else:
            stats["unchanged"] += 1
    return stats


def _import_history(
    store: StateStore,
    conn: sqlite3.Connection,
    path: Path,
    file_sha: str,
    imported_at: str,
) -> dict[str, int]:
    stats = {
        "events": 0,
        "skipped": 0,
        "lines": 0,
        "history_events": 0,
        "history_events_skipped": 0,
    }
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StorageError(f"unreadable history at {path}: {exc}") from exc

    store._insert_legacy_import(
        conn,
        source_kind="history_file",
        source_path=str(path),
        source_sha256=file_sha,
        record_key=f"file:{file_sha}",
        payload={"line_count_hint": text.count("\n") + (1 if text and not text.endswith("\n") else 0)},
        imported_at=imported_at,
    )

    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        stats["lines"] += 1
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"malformed history line {line_no} at {path}: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise StorageError(f"history line {line_no} at {path} is not an object")
        # Source identity is line position + content hash so every JSONL row
        # survives even when two lines share the same event_id payload field.
        line_digest = sha256_text(stripped)
        record_key = f"line:{line_no}:{line_digest}"
        # Preserve event_id as data; do not use it as the import uniqueness key.
        if "event_id" not in event or not str(event.get("event_id") or "").strip():
            event = {
                **event,
                "event_id": f"line:{line_no}:{line_digest[:16]}",
            }
        inserted = store._insert_legacy_import(
            conn,
            source_kind="history",
            source_path=str(path),
            source_sha256=file_sha,
            record_key=record_key,
            payload=event,
            imported_at=imported_at,
        )
        if inserted:
            stats["events"] += 1
        else:
            stats["skipped"] += 1

        # Runtime journal: same all-or-nothing migration transaction. Dedupe is
        # record_key-based so duplicate event_id lines remain distinct history rows.
        dedupe = legacy_history_record_dedupe_key(record_key)
        _event, created = store._append_history_event_in_tx(
            conn,
            event,
            dedupe_key=dedupe,
            created_at=imported_at,
            require_outcome=False,
        )
        if created:
            stats["history_events"] += 1
        else:
            stats["history_events_skipped"] += 1
    return stats


def _backfill_history_from_legacy_imports(
    store: StateStore,
    conn: sqlite3.Connection,
    *,
    imported_at: str,
) -> dict[str, int]:
    """Populate history_events from already-imported legacy history rows.

    Does not read or modify original JSONL. Preserves each legacy_imports
    ``record_key`` as a distinct journal row (duplicate event_id lines survive).
    """

    stats = {
        "history_events": 0,
        "history_events_skipped": 0,
        "source_rows": 0,
        "source": "legacy_imports",
    }
    rows = conn.execute(
        """
        SELECT record_key, payload_json
        FROM legacy_imports
        WHERE source_kind = 'history'
        ORDER BY id ASC
        """
    ).fetchall()
    for row in rows:
        stats["source_rows"] += 1
        record_key = str(row["record_key"])
        try:
            event = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError(
                f"corrupt legacy_imports history payload for {record_key!r}: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise StorageError(
                f"legacy_imports history payload for {record_key!r} is not an object"
            )
        if "event_id" not in event or not str(event.get("event_id") or "").strip():
            digest = sha256_text(row["payload_json"] or "")
            event = {**event, "event_id": f"legacy:{record_key}:{digest[:16]}"}
        dedupe = legacy_history_record_dedupe_key(record_key)
        _event, created = store._append_history_event_in_tx(
            conn,
            event,
            dedupe_key=dedupe,
            created_at=imported_at,
            require_outcome=False,
        )
        if created:
            stats["history_events"] += 1
        else:
            stats["history_events_skipped"] += 1
    return stats


def _write_migration_receipt(
    conn: sqlite3.Connection,
    *,
    name: str,
    completed_at: str,
    backup_dir: Any,
    manifest: Any,
    stats: Any,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO migration_receipts (
            name, completed_at, backup_dir, manifest_json, stats_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            name,
            completed_at,
            backup_dir,
            canonical_json(manifest if manifest is not None else {}),
            canonical_json(stats if stats is not None else {}),
        ),
    )


def _classify_legacy_grinder_recovery(
    payload: Mapping[str, Any],
) -> tuple[bool, str]:
    """Classify legacy grinder-rest JSON into terminal cooldown vs active recovery.

    Terminal (``cooldown_imported``) when:
    - ``in_progress`` is explicitly false, or
    - ``stopped_at`` is present, or
    - status/phase is a known terminal rest/stop/complete/idle/cancelled token.

    Non-terminal (``recovery_imported``, fail-closed) when:
    - ``in_progress`` is true, or
    - a reserve-style record (``reserved_at`` without confirmed stop), or
    - semantic state is unknown/malformed for a safe cooldown claim.
    """

    if payload.get("in_progress") is True:
        return False, "recovery_imported"
    if payload.get("in_progress") is False:
        return True, "cooldown_imported"
    if payload.get("stopped_at") is not None:
        return True, "cooldown_imported"
    status = str(payload.get("status") or payload.get("phase") or "").strip().lower()
    if status in LEGACY_GRINDER_TERMINAL_STATUSES:
        return True, "cooldown_imported"
    # Reserve-before-start records lack confirmed stop; fail closed.
    if payload.get("reserved_at") is not None and payload.get("stopped_at") is None:
        return False, "recovery_imported"
    # Unknown / empty semantic state: never claim a confirmed cooldown.
    return False, "recovery_imported"


def _import_recovery(
    store: StateStore,
    conn: sqlite3.Connection,
    *,
    kind: str,
    path: Path,
    file_sha: str,
    imported_at: str,
) -> dict[str, int]:
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"malformed recovery file at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StorageError(f"recovery file at {path} must be an object")

    record_key = f"recovery:{kind}:{file_sha}"
    inserted = store._insert_legacy_import(
        conn,
        source_kind=kind,
        source_path=str(path),
        source_sha256=file_sha,
        record_key=record_key,
        payload=payload,
        imported_at=imported_at,
    )
    stats = {"records": 1 if inserted else 0, "skipped": 0 if inserted else 1}
    if not inserted:
        return stats

    # Surface recovery as a workflow row. Coffee/tea always non-terminal
    # recovery_imported. Grinder may be terminal cooldown_imported when the
    # legacy record already encodes a confirmed stop / rest interval.
    workflow_kind = {
        "recovery_armed": "coffee_recovery",
        "recovery_tea": "tea_recovery",
        "recovery_grinder": "grinder_recovery",
    }.get(kind, kind)
    wid = f"legacy_{kind}_{file_sha[:16]}"
    if kind == "recovery_grinder":
        is_terminal, state = _classify_legacy_grinder_recovery(payload)
    else:
        is_terminal, state = False, "recovery_imported"
    terminal_at = imported_at if is_terminal else None
    conn.execute(
        """
        INSERT OR IGNORE INTO workflows (
            workflow_id, kind, state, recipe_revision_id, snapshot_json,
            snapshot_sha256, source, owner, machine_phase, recovery_json,
            created_at, updated_at, terminal_at, metadata_json
        ) VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        (
            wid,
            workflow_kind,
            state,
            "legacy_migration",
            canonical_json(payload),
            imported_at,
            imported_at,
            terminal_at,
            canonical_json(
                {
                    "legacy_path": str(path),
                    "legacy_sha256": file_sha,
                    "source_kind": kind,
                    "import_classification": state,
                }
            ),
        ),
    )
    stats["terminal"] = 1 if is_terminal else 0
    stats["state"] = state
    return stats


def _assert_single_nonterminal_recovery(
    conn: sqlite3.Connection,
) -> None:
    """Fail closed when import would leave multiple concurrent recovery activities."""

    rows = conn.execute(
        f"""
        SELECT workflow_id, kind, metadata_json FROM workflows
        WHERE terminal_at IS NULL
          AND kind IN ({", ".join("?" for _ in RECOVERY_WORKFLOW_KINDS)})
        ORDER BY created_at ASC, workflow_id ASC
        """,
        tuple(sorted(RECOVERY_WORKFLOW_KINDS)),
    ).fetchall()
    if len(rows) <= 1:
        return
    source_kinds: list[str] = []
    for row in rows:
        meta_raw = row["metadata_json"] or "{}"
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            meta = {}
        source = meta.get("source_kind") if isinstance(meta, dict) else None
        source_kinds.append(str(source or row["kind"]))
    raise StorageError(
        "legacy migration would leave more than one non-terminal recovery "
        f"workflow from source kinds {source_kinds}; refuse to choose one "
        "activity (entire import rolled back; originals and backup remain)"
    )


def _runtime_truth_full() -> dict[str, str]:
    return {
        "workflow": "sqlite",
        "history": "sqlite",
        "idempotency": "sqlite",
        "catalog": "sqlite",
    }


def _sqlite_active_for_full() -> list[str]:
    return ["workflow", "history", "idempotency", "catalog"]


def migrate_legacy_state(
    state_root: Path | str,
    *,
    store: StateStore | None = None,
    backup_root: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """One-time lossless import of legacy JSON/JSONL into state.db.

    - Creates a timestamped backup with byte-identical copies + manifest.
    - Never deletes or modifies originals.
    - Imports **only** from the immutable backup copies (not live originals),
      so concurrent mutation of sources cannot affect the DB transaction.
    - Runs imports in a single DB transaction; failures roll back rows and
      do not mark the migration complete.
    - Reruns after success are idempotent (no duplicate data).

    Three independent receipts:
    - ``legacy_json_v1``: raw legacy import into ``legacy_imports`` (+ catalog/
      recovery side effects).
    - ``legacy_history_sqlite_v1``: history journal cutover into
      ``history_events``.
    - ``legacy_catalog_sqlite_v1``: catalog runtime cutover into
      ``recipes`` / ``recipe_revisions`` with full entry envelopes.

    On a fresh migrate all receipts commit atomically. On an already-completed
    older import, a normal migrate (no ``--force``) backfills missing cutovers
    from ``legacy_imports`` without rereading catalog.json or brew-history.jsonl.

    After cutover, runtime truth for workflow/history/idempotency/catalog is
    SQLite. Legacy JSON files remain import-only and are never rewritten.
    """

    root = normalize_state_root(state_root)
    # Close only stores we create. Caller-supplied ``store=`` (e.g. open_store)
    # stays open for the caller to manage -- critical on Windows where an open
    # SQLite handle blocks rename/delete of state.db until GC would free it.
    owns_store = store is None
    db = store if store is not None else StateStore(root)
    try:
        db.ensure_schema()

        legacy_done = db.migration_completed(LEGACY_MIGRATION_NAME)
        history_done = db.migration_completed(LEGACY_HISTORY_CUTOVER_NAME)
        catalog_done = db.migration_completed(LEGACY_CATALOG_CUTOVER_NAME)
        runtime_truth = _runtime_truth_full()
        active_for = _sqlite_active_for_full()

        if legacy_done and history_done and catalog_done and not force:
            receipt = db.get_migration_receipt(LEGACY_MIGRATION_NAME) or {}
            history_receipt = (
                db.get_migration_receipt(LEGACY_HISTORY_CUTOVER_NAME) or {}
            )
            catalog_receipt = (
                db.get_migration_receipt(LEGACY_CATALOG_CUTOVER_NAME) or {}
            )
            return {
                "status": "already_completed",
                "state_root": str(root),
                "receipt": receipt,
                "history_cutover_receipt": history_receipt,
                "catalog_cutover_receipt": catalog_receipt,
                "migration_name": LEGACY_MIGRATION_NAME,
                "history_cutover_name": LEGACY_HISTORY_CUTOVER_NAME,
                "catalog_cutover_name": LEGACY_CATALOG_CUTOVER_NAME,
                "migration_completed": True,
                "history_cutover_completed": True,
                "catalog_cutover_completed": True,
                "imported": False,
                "runtime_source_of_truth": runtime_truth,
                "sqlite_active_for": active_for,
                "sqlite_active_runtime": True,
                "catalog_runtime": "sqlite",
                "history_runtime": "sqlite",
                "message": (
                    "legacy import, history cutover, and catalog cutover already "
                    "completed; SQLite is active for workflow/history/idempotency/"
                    "catalog"
                ),
            }

        # Old-receipt path: legacy_json_v1 done but one or more cutovers missing.
        # Backfill from legacy_imports only; never reread originals.
        if legacy_done and (not history_done or not catalog_done) and not force:
            imported_at = utc_now()
            cutover_stats: dict[str, Any] = {}
            catalog_stats: dict[str, Any] = {}
            try:
                with db.transaction() as conn:
                    if not history_done:
                        _maybe_fault("in_history_cutover_start")
                        cutover_stats = _backfill_history_from_legacy_imports(
                            db, conn, imported_at=imported_at
                        )
                        _write_migration_receipt(
                            conn,
                            name=LEGACY_HISTORY_CUTOVER_NAME,
                            completed_at=imported_at,
                            backup_dir=None,
                            manifest={
                                "kind": "history_cutover",
                                "source": "legacy_imports",
                                "from_migration": LEGACY_MIGRATION_NAME,
                            },
                            stats=cutover_stats,
                        )
                    if not catalog_done:
                        _maybe_fault("in_catalog_cutover_start")
                        catalog_stats = _backfill_catalog_from_legacy_imports(
                            db, conn, imported_at=imported_at
                        )
                        _write_migration_receipt(
                            conn,
                            name=LEGACY_CATALOG_CUTOVER_NAME,
                            completed_at=imported_at,
                            backup_dir=None,
                            manifest={
                                "kind": "catalog_cutover",
                                "source": "legacy_imports",
                                "from_migration": LEGACY_MIGRATION_NAME,
                            },
                            stats=catalog_stats,
                        )
                    _maybe_fault("before_commit")
            except Exception:
                raise
            receipt = db.get_migration_receipt(LEGACY_MIGRATION_NAME) or {}
            history_receipt = (
                db.get_migration_receipt(LEGACY_HISTORY_CUTOVER_NAME) or {}
            )
            catalog_receipt = (
                db.get_migration_receipt(LEGACY_CATALOG_CUTOVER_NAME) or {}
            )
            stats_out: dict[str, Any] = {}
            if cutover_stats:
                stats_out["history_cutover"] = cutover_stats
            if catalog_stats:
                stats_out["catalog_cutover"] = catalog_stats
            return {
                "status": "completed",
                "state_root": str(root),
                "receipt": receipt,
                "history_cutover_receipt": history_receipt,
                "catalog_cutover_receipt": catalog_receipt,
                "migration_name": LEGACY_MIGRATION_NAME,
                "history_cutover_name": LEGACY_HISTORY_CUTOVER_NAME,
                "catalog_cutover_name": LEGACY_CATALOG_CUTOVER_NAME,
                "migration_completed": True,
                "history_cutover_completed": True,
                "catalog_cutover_completed": True,
                "imported": False,
                "history_backfilled": bool(cutover_stats),
                "catalog_backfilled": bool(catalog_stats),
                "stats": stats_out,
                "completed_at": imported_at,
                "runtime_source_of_truth": runtime_truth,
                "sqlite_active_for": active_for,
                "sqlite_active_runtime": True,
                "catalog_runtime": "sqlite",
                "history_runtime": "sqlite",
                "message": (
                    "missing cutovers backfilled from legacy_imports; SQLite is "
                    "active for workflow/history/idempotency/catalog"
                ),
            }

        # Backup always (empty backup is valid when no legacy files exist).
        # Import sources and files_seen are derived *only* from this completed,
        # validated backup manifest -- never from a pre-backup live listing, which
        # can race with files appearing between enumeration and copy.
        backup_manifest = create_legacy_backup(
            root,
            backup_root=Path(backup_root) if backup_root is not None else None,
        )
        _maybe_fault("after_backup")
        backup_dir = Path(str(backup_manifest["backup_dir"]))

        kind_by_rel = {rel.as_posix(): kind for kind, rel in LEGACY_SOURCES}
        import_sources: list[tuple[str, Path, str]] = []
        seen_kinds: list[str] = []
        seen_kind_set: set[str] = set()
        for entry in backup_manifest.get("files") or []:
            if not isinstance(entry, dict):
                raise StorageError("backup manifest file entry must be an object")
            kind = entry.get("kind")
            rel_s = entry.get("relative_path")
            digest = entry.get("sha256")
            if not isinstance(kind, str) or not kind:
                raise StorageError("backup manifest entry missing kind")
            if not isinstance(rel_s, str) or not rel_s:
                raise StorageError("backup manifest entry missing relative_path")
            if not isinstance(digest, str) or not digest:
                raise StorageError(f"backup manifest entry for {rel_s} missing sha256")
            expected_kind = kind_by_rel.get(rel_s)
            if expected_kind is None:
                raise StorageError(
                    f"backup manifest contains unexpected path {rel_s!r}"
                )
            if kind != expected_kind:
                raise StorageError(
                    f"backup manifest kind/path mismatch for {rel_s}: "
                    f"got {kind!r}, expected {expected_kind!r}"
                )
            if kind in seen_kind_set:
                raise StorageError(
                    f"backup manifest lists source kind {kind!r} more than once"
                )
            backup_path = backup_dir / Path(rel_s)
            if not backup_path.is_file():
                raise StorageError(
                    f"backup missing expected file {rel_s} under {backup_dir}"
                )
            file_sha = sha256_file(backup_path)
            if file_sha != digest:
                raise StorageError(
                    f"backup integrity failure for {rel_s}: digest mismatch"
                )
            import_sources.append((kind, backup_path, file_sha))
            seen_kinds.append(kind)
            seen_kind_set.add(kind)

        imported_at = utc_now()
        stats: dict[str, Any] = {
            "catalog": {},
            "history": {},
            "recovery_armed": {},
            "recovery_tea": {},
            "recovery_grinder": {},
            "files_seen": list(seen_kinds),
            "import_from": "backup_copies",
        }

        try:
            with db.transaction() as conn:
                _maybe_fault("in_transaction_start")
                for kind, path, file_sha in import_sources:
                    if kind == "catalog":
                        stats["catalog"] = _import_catalog(
                            db, conn, path, file_sha, imported_at
                        )
                    elif kind == "history":
                        stats["history"] = _import_history(
                            db, conn, path, file_sha, imported_at
                        )
                    else:
                        stats[kind] = _import_recovery(
                            db,
                            conn,
                            kind=kind,
                            path=path,
                            file_sha=file_sha,
                            imported_at=imported_at,
                        )
                    _maybe_fault(f"after_{kind}")

                # Never leave multiple concurrent recovery activities after import.
                _assert_single_nonterminal_recovery(conn)

                # All receipts + imported rows commit atomically.
                _write_migration_receipt(
                    conn,
                    name=LEGACY_MIGRATION_NAME,
                    completed_at=imported_at,
                    backup_dir=backup_manifest.get("backup_dir"),
                    manifest=backup_manifest,
                    stats=stats,
                )
                history_cutover_stats = {
                    "history_events": int(
                        (stats.get("history") or {}).get("history_events") or 0
                    ),
                    "history_events_skipped": int(
                        (stats.get("history") or {}).get("history_events_skipped")
                        or 0
                    ),
                    "source": "legacy_import_transaction",
                    "files_seen": list(seen_kinds),
                }
                _write_migration_receipt(
                    conn,
                    name=LEGACY_HISTORY_CUTOVER_NAME,
                    completed_at=imported_at,
                    backup_dir=backup_manifest.get("backup_dir"),
                    manifest={
                        "kind": "history_cutover",
                        "source": "legacy_import_transaction",
                        "paired_with": LEGACY_MIGRATION_NAME,
                    },
                    stats=history_cutover_stats,
                )
                stats["history_cutover"] = history_cutover_stats
                catalog_cutover_stats = {
                    "entries": int((stats.get("catalog") or {}).get("entries") or 0),
                    "recipes": int((stats.get("catalog") or {}).get("recipes") or 0),
                    "revisions": int(
                        (stats.get("catalog") or {}).get("revisions") or 0
                    ),
                    "source": "legacy_import_transaction",
                    "files_seen": list(seen_kinds),
                }
                _write_migration_receipt(
                    conn,
                    name=LEGACY_CATALOG_CUTOVER_NAME,
                    completed_at=imported_at,
                    backup_dir=backup_manifest.get("backup_dir"),
                    manifest={
                        "kind": "catalog_cutover",
                        "source": "legacy_import_transaction",
                        "paired_with": LEGACY_MIGRATION_NAME,
                    },
                    stats=catalog_cutover_stats,
                )
                stats["catalog_cutover"] = catalog_cutover_stats
                _maybe_fault("before_commit")
        except Exception:
            # Transaction rolled back by context manager; originals untouched.
            raise

        return {
            "status": "completed",
            "state_root": str(root),
            "backup": backup_manifest,
            "stats": stats,
            "imported": True,
            "completed_at": imported_at,
            "migration_name": LEGACY_MIGRATION_NAME,
            "history_cutover_name": LEGACY_HISTORY_CUTOVER_NAME,
            "catalog_cutover_name": LEGACY_CATALOG_CUTOVER_NAME,
            "migration_completed": True,
            "history_cutover_completed": True,
            "catalog_cutover_completed": True,
            "runtime_source_of_truth": runtime_truth,
            "sqlite_active_for": active_for,
            "sqlite_active_runtime": True,
            "catalog_runtime": "sqlite",
            "history_runtime": "sqlite",
            "message": (
                "legacy JSON/JSONL imported into state.db with history and "
                "catalog cutover; SQLite is active for workflow/history/"
                "idempotency/catalog"
            ),
        }
    finally:
        if owns_store:
            db.close()


def migration_status(state_root: Path | str | None = None) -> dict[str, Any]:
    """Observable migration/runtime truth for operators and CLI status."""

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else resolve_state_dir()
    )
    store = StateStore(root)
    try:
        store.ensure_schema()
        completed = store.migration_completed(LEGACY_MIGRATION_NAME)
        history_cutover = store.migration_completed(LEGACY_HISTORY_CUTOVER_NAME)
        catalog_cutover = store.migration_completed(LEGACY_CATALOG_CUTOVER_NAME)
        receipt = (
            store.get_migration_receipt(LEGACY_MIGRATION_NAME) if completed else None
        )
        history_receipt = (
            store.get_migration_receipt(LEGACY_HISTORY_CUTOVER_NAME)
            if history_cutover
            else None
        )
        catalog_receipt = (
            store.get_migration_receipt(LEGACY_CATALOG_CUTOVER_NAME)
            if catalog_cutover
            else None
        )
        present = [
            {"kind": kind, "path": str(root / rel), "exists": (root / rel).is_file()}
            for kind, rel in LEGACY_SOURCES
        ]
        cutovers_complete = history_cutover and catalog_cutover
        return {
            "state_root": str(root),
            "db_path": str(store.db_path),
            "migration_name": LEGACY_MIGRATION_NAME,
            "migration_completed": completed,
            "receipt": receipt,
            "history_cutover_name": LEGACY_HISTORY_CUTOVER_NAME,
            "history_cutover_completed": history_cutover,
            "history_cutover_receipt": history_receipt,
            "catalog_cutover_name": LEGACY_CATALOG_CUTOVER_NAME,
            "catalog_cutover_completed": catalog_cutover,
            "catalog_cutover_receipt": catalog_receipt,
            "legacy_sources": present,
            "runtime_source_of_truth": _runtime_truth_full(),
            "sqlite_active_runtime": True,
            "sqlite_active_for": _sqlite_active_for_full(),
            "catalog_runtime": "sqlite",
            "history_runtime": "sqlite",
            "message": (
                "state.db is the authoritative runtime store for workflow, history, "
                "idempotency, and catalog (recipes/recipe_revisions). Legacy "
                "catalog.json and brew-history.jsonl are import-only. "
                f"History cutover ({LEGACY_HISTORY_CUTOVER_NAME}) "
                f"{'is complete' if history_cutover else 'is pending'}; "
                f"catalog cutover ({LEGACY_CATALOG_CUTOVER_NAME}) "
                f"{'is complete' if catalog_cutover else 'is pending'}."
                + (
                    ""
                    if cutovers_complete
                    else " Run xbloom-state migrate to finish pending cutovers."
                )
            ),
        }
    finally:
        store.close()


def open_store(
    state_root: Path | str | None = None,
    *,
    migrate: bool = False,
) -> StateStore:
    """Open (and optionally migrate) the store for a state root.

    ``migrate=True`` runs the explicit one-shot import helper. Daemon startup
    should not pass ``migrate=True`` (migration remains an explicit operator
    action). Workflow/history/idempotency/catalog runtime uses SQLite.

    On success the open store is returned for the caller to manage. On any
    exception before return the store is closed (Windows holds SQLite locks
    until ``close()``; do not rely on GC).
    """

    store = StateStore(state_root)
    try:
        store.ensure_schema()
        if migrate:
            migrate_legacy_state(store.state_root, store=store)
        return store
    except Exception:
        store.close()
        raise


def main(argv: list[str] | None = None) -> None:
    """CLI: state migration / status / online backup (explicit, never auto)."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="xbloom-state",
        description=(
            "Explicit state.db maintenance. Migration is idempotent. SQLite is "
            "active for workflow/history/idempotency/catalog."
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="state root (default: XBLOOM_STATE_DIR / legacy alias / home default)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status",
        help="show migration receipt and runtime source-of-truth contract",
    )
    migrate_p = sub.add_parser(
        "migrate",
        help="backup legacy JSON/JSONL and import into state.db (idempotent)",
    )
    migrate_p.add_argument(
        "--force",
        action="store_true",
        help="re-run import transaction even if a receipt exists (still idempotent keys)",
    )
    migrate_p.add_argument(
        "--backup-root",
        default=None,
        help="directory for the pre-migration backup tree",
    )
    backup_p = sub.add_parser(
        "backup",
        help="online SQLite backup of state.db (does not migrate)",
    )
    backup_p.add_argument(
        "--destination",
        default=None,
        help="optional destination .db path (must not already exist)",
    )

    args = parser.parse_args(argv)
    root = Path(args.state_dir) if args.state_dir else None
    if args.command == "status":
        result = migration_status(root)
    elif args.command == "migrate":
        target = normalize_state_root(root) if root is not None else resolve_state_dir()
        result = migrate_legacy_state(
            target,
            backup_root=Path(args.backup_root) if args.backup_root else None,
            force=bool(args.force),
        )
    elif args.command == "backup":
        store = StateStore(root)
        try:
            store.ensure_schema()
            dest = store.backup(
                Path(args.destination) if args.destination else None
            )
            result = {
                "status": "backed_up",
                "destination": str(dest),
                "state_root": str(store.state_root),
                "runtime_source_of_truth": _runtime_truth_full(),
                "sqlite_active_for": _sqlite_active_for_full(),
            }
        finally:
            store.close()
    else:  # pragma: no cover
        parser.error(f"unknown command {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


__all__ = [
    "ACTIVE_WORKFLOW_STATES",
    "BUSY_TIMEOUT_MS",
    "CATALOG_ENVELOPE_RUNTIME_KEYS",
    "CATALOG_SOURCE_LEGACY",
    "CATALOG_SOURCE_MERGE",
    "CATALOG_SOURCE_SKILL",
    "CATALOG_SOURCE_WEB",
    "CLEAR_RECOVERY",
    "DB_FILE_NAME",
    "DEFAULT_HISTORY_LIST_LIMIT",
    "DEFAULT_RECIPE_LIST_LIMIT",
    "GRINDER_WORKFLOW_KINDS",
    "HISTORY_SOURCE_APP",
    "HISTORY_SOURCE_LOCAL",
    "HISTORY_VALID_OUTCOMES",
    "IDEM_COMPLETED",
    "IDEM_FAILED",
    "IDEM_PENDING",
    "KNOWN_WORKFLOW_KINDS",
    "LEGACY_CATALOG_CUTOVER_NAME",
    "LEGACY_GRINDER_TERMINAL_STATUSES",
    "LEGACY_HISTORY_CUTOVER_NAME",
    "LEGACY_MIGRATION_NAME",
    "MAX_HISTORY_LIST_LIMIT",
    "MAX_HISTORY_NOTE_CHARS",
    "MAX_RECIPE_LIST_LIMIT",
    "RECOVERY_WORKFLOW_KINDS",
    "SCHEMA_VERSION",
    "StateStore",
    "StorageConflictError",
    "StorageError",
    "canonicalize_recipe_content",
    "canonical_json",
    "catalog_entry_id_from_recipe_id",
    "catalog_ownership_entry_ids",
    "content_sha256",
    "default_db_path",
    "history_event_dedupe_key",
    "history_event_from_workflow_terminal",
    "legacy_history_line_dedupe_key",
    "legacy_history_record_dedupe_key",
    "map_terminal_history_outcome",
    "merge_catalog_envelopes",
    "migrate_legacy_state",
    "migration_status",
    "new_history_event_id",
    "normalize_catalog_envelope",
    "open_store",
    "public_history_event",
    "recipe_id_for_catalog_entry_id",
    "reject_forbidden_provenance",
    "sanitize_recipe_provenance",
    "sha256_bytes",
    "split_catalog_entry",
    "sha256_file",
    "sha256_text",
    "utc_now",
    "workflow_terminal_history_dedupe_key",
]


if __name__ == "__main__":
    main()

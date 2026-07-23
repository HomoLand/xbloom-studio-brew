"""Local brew journal facade backed by StateStore / state.db.

Phase 0.3/0.4 history cutover: the append-only runtime journal lives in the
``history_events`` table. This module keeps the public API used by Skill CLI
and tests, but never appends or rewrites ``brew-history.jsonl``.

``XBLOOM_HISTORY_PATH`` remains only as a deprecated state-root selector
(when it points at a legacy JSONL file, the parent directory is the state root).
Default resolution uses ``XBLOOM_STATE_DIR``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from xbloom_paths import environment_value, skill_state_dir
from xbloom_storage import (
    HISTORY_SOURCE_APP as SOURCE_APP,
    HISTORY_SOURCE_LOCAL as SOURCE_LOCAL,
    HISTORY_VALID_OUTCOMES as VALID_OUTCOMES,
    MAX_HISTORY_LIST_LIMIT as MAX_LIST_HARD,
    MAX_HISTORY_NOTE_CHARS as MAX_NOTE_CHARS,
    DEFAULT_HISTORY_LIST_LIMIT as MAX_LIST_DEFAULT,
    StateStore,
    StorageError,
    history_event_dedupe_key,
    new_history_event_id as new_event_id,
    public_history_event as _public_event,
    utc_now as storage_utc_now,
)


SCHEMA_VERSION = 1
HISTORY_PATH_ENV = "XBLOOM_HISTORY_PATH"
HISTORY_FILE_NAME = "brew-history.jsonl"
DB_FILE_NAME = "state.db"


class HistoryError(RuntimeError):
    """Raised for malformed history input or invalid journal writes."""


def utc_now() -> str:
    return storage_utc_now()


def _state_root_from_path(path: Path) -> Path:
    """Map an explicit path to the associated state root.

    - Directory path -> that directory
    - ``brew-history.jsonl`` (or any file) -> parent directory
    - ``state.db`` file -> parent directory
    """

    resolved = path.expanduser()
    if resolved.name == DB_FILE_NAME or resolved.suffix.lower() in {".db", ".jsonl"}:
        return resolved.parent
    if resolved.name == HISTORY_FILE_NAME:
        return resolved.parent
    # Prefer treating existing directories as state roots.
    if resolved.is_dir() or not resolved.suffix:
        return resolved
    return resolved.parent


def resolve_history_state_root(path: str | Path | None = None) -> Path:
    """Resolve the state root used for history reads/writes.

    Precedence for default (path is None):
    1. Deprecated ``XBLOOM_HISTORY_PATH`` as state-root selector
    2. ``XBLOOM_STATE_DIR`` / skill state dir
    """

    if path is not None:
        return _state_root_from_path(Path(path))
    configured = environment_value(HISTORY_PATH_ENV)
    if configured:
        return _state_root_from_path(Path(configured))
    return skill_state_dir()


def default_history_path(state_dir: Path | None = None) -> Path:
    """Legacy path helper: returns the historical JSONL location under the state root.

    This path is **not** a write target after the SQLite cutover. Use
    :func:`history_summary` / StateStore for the authoritative ``state.db`` path.
    """

    if state_dir is not None:
        root = Path(state_dir)
    else:
        root = resolve_history_state_root()
    return root / HISTORY_FILE_NAME


def _store_for(path: str | Path | None = None) -> StateStore:
    return StateStore(resolve_history_state_root(path))


def _map_storage_error(exc: StorageError) -> HistoryError:
    return HistoryError(str(exc))


def _clean_text(value: Any, *, field: str, max_chars: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise HistoryError(f"{field} must be at most {max_chars} characters")
    return text


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise HistoryError(f"expected number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HistoryError(f"expected number, got {value!r}") from exc
    if number != number:  # NaN
        raise HistoryError("expected finite number")
    return number


def _optional_int(value: Any) -> int | None:
    number = _optional_number(value)
    if number is None:
        return None
    if abs(number - round(number)) > 1e-9:
        raise HistoryError(f"expected whole number, got {value!r}")
    return int(round(number))


def append_event(
    event: Mapping[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Append one journal event to state.db. Never writes JSONL."""

    store = _store_for(path)
    try:
        payload = _public_event(event)
        if "event_id" not in payload:
            payload["event_id"] = new_event_id()
        return store.append_history_event(
            payload,
            dedupe_key=history_event_dedupe_key(str(payload["event_id"])),
        )
    except StorageError as exc:
        raise _map_storage_error(exc) from exc
    finally:
        store.close()


def load_events(path: str | Path | None = None) -> list[dict[str, Any]]:
    store = _store_for(path)
    try:
        return store.load_history_events()
    except StorageError as exc:
        raise _map_storage_error(exc) from exc
    finally:
        store.close()


def list_events(
    *,
    path: str | Path | None = None,
    limit: int = MAX_LIST_DEFAULT,
    source: str | None = None,
    outcome: str | None = None,
    query: str | None = None,
    recipe_sha256: str | None = None,
) -> list[dict[str, Any]]:
    store = _store_for(path)
    try:
        return store.list_history_events(
            limit=limit,
            source=source,
            outcome=outcome,
            query=query,
            recipe_sha256=recipe_sha256,
        )
    except StorageError as exc:
        raise _map_storage_error(exc) from exc
    finally:
        store.close()


def history_summary(path: str | Path | None = None) -> dict[str, Any]:
    """Summary with SQLite state.db as the authoritative path/source."""

    store = _store_for(path)
    try:
        summary = store.history_summary()
        # Be explicit for operators and CLI consumers.
        summary.setdefault("source", "state.db")
        summary.setdefault("authoritative", "sqlite")
        return summary
    except StorageError as exc:
        raise _map_storage_error(exc) from exc
    finally:
        store.close()


def add_note(
    event_id: str,
    note: str,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Attach a tasting/operator note by appending a linked note event."""

    store = _store_for(path)
    try:
        return store.add_history_note(event_id, note)
    except StorageError as exc:
        raise _map_storage_error(exc) from exc
    finally:
        store.close()


def event_from_workflow(
    *,
    command: str,
    outcome: str,
    state: Mapping[str, Any] | None = None,
    summary: Mapping[str, Any] | None = None,
    monitor: Mapping[str, Any] | None = None,
    error: str | None = None,
    note: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a local journal event from CLI workflow state and telemetry.

    Kept for compatibility (notes / non-bridge tooling). Bridge-owned terminal
    history is derived inside ``StateStore.commit_workflow_terminal``.
    """

    state = dict(state or {})
    summary = dict(summary or {})
    monitor = dict(monitor or {})
    event: dict[str, Any] = {
        "event_kind": "workflow",
        "command": command,
        "outcome": outcome,
        "source": SOURCE_LOCAL,
        "recipe_path": state.get("recipe_path") or summary.get("recipe_path"),
        "recipe_sha256": state.get("recipe_sha256") or summary.get("recipe_sha256"),
        "recipe_name": summary.get("name") or state.get("recipe_name"),
        "machine": state.get("machine"),
        "address": state.get("address"),
        "firmware": state.get("firmware"),
        "serving_kind": state.get("serving_kind") or summary.get("kind"),
        "machine_program": state.get("machine_program") or summary.get("machine_program"),
        "manual_preload_ice_g": state.get("manual_preload_ice_g")
        if state.get("manual_preload_ice_g") is not None
        else summary.get("manual_preload_ice_g"),
        "target_dispensed_water_ml": state.get("target_dispensed_water_ml")
        if state.get("target_dispensed_water_ml") is not None
        else summary.get("target_dispensed_water_ml")
        or summary.get("programmed_water_ml"),
        "dose_g": summary.get("dose_g") or summary.get("leaf_g"),
        "grind": summary.get("grind"),
        "hot_water_ml": summary.get("hot_water_ml"),
        "final_water_ml": summary.get("final_water_ml"),
        "ice_g": summary.get("ice_g"),
        "pours": summary.get("pours") or summary.get("steeps"),
    }
    if "loaded_at" in state:
        event["loaded_at"] = state.get("loaded_at")
    if "started_at" in state:
        event["started_at"] = state.get("started_at")
    for key in (
        "completion_confirmed",
        "terminal_confirmed",
        "terminal_state",
        "last_state",
        "dispensed_water_ml",
        "cup_weight_g",
        "cup_delta_g",
        "elapsed_s",
        "events_seen",
        "tea_phase",
        "pour_stage",
        "errors",
        "dispensed_vs_target_ml",
        "cup_delta_to_dispensed_ratio",
    ):
        if key in monitor and monitor.get(key) is not None:
            event[key] = monitor.get(key)
    if error:
        event["error"] = str(error)
    if note:
        event["note"] = _clean_text(note, field="note", max_chars=MAX_NOTE_CHARS)
    if extra:
        for key, value in extra.items():
            if value is not None and key not in event:
                event[key] = value
    # Drop empty optional fields for a compact journal.
    return {key: value for key, value in event.items() if value not in (None, "", [], {})}


def import_app_records(
    records: list[Mapping[str, Any]],
    *,
    path: str | Path | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Import normalised App brew-record dicts into state.db history."""

    store = _store_for(path)
    try:
        return store.import_app_history_records(records, region=region)
    except StorageError as exc:
        raise _map_storage_error(exc) from exc
    finally:
        store.close()


__all__ = [
    "HISTORY_PATH_ENV",
    "SOURCE_APP",
    "SOURCE_LOCAL",
    "VALID_OUTCOMES",
    "HistoryError",
    "add_note",
    "append_event",
    "default_history_path",
    "event_from_workflow",
    "history_summary",
    "import_app_records",
    "list_events",
    "load_events",
    "new_event_id",
    "resolve_history_state_root",
    "utc_now",
]

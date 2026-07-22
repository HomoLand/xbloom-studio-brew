"""Local brew journal for dial-in and telemetry replay.

The Skill keeps an append-only JSONL journal under the user state directory so
Hermes/Codex can review actual machine runs without relying on chat history.
App-side brew records can be imported into the same journal as a separate
source, because phone-only sessions never produce local BLE telemetry.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Mapping
from uuid import uuid4

from xbloom_paths import environment_value, skill_state_dir


SCHEMA_VERSION = 1
HISTORY_PATH_ENV = "XBLOOM_HISTORY_PATH"
HISTORY_FILE_NAME = "brew-history.jsonl"
MAX_NOTE_CHARS = 500
MAX_LIST_DEFAULT = 20
MAX_LIST_HARD = 200
SOURCE_LOCAL = "local-skill"
SOURCE_APP = "app-cloud"
VALID_OUTCOMES = frozenset(
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


class HistoryError(RuntimeError):
    """Raised for malformed history files or invalid journal writes."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_history_path(state_dir: Path | None = None) -> Path:
    configured = environment_value(HISTORY_PATH_ENV)
    if configured:
        return Path(configured).expanduser()
    root = Path(state_dir) if state_dir is not None else skill_state_dir()
    return root / HISTORY_FILE_NAME


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


def new_event_id() -> str:
    return f"bh_{uuid4().hex}"


def _public_event(event: Mapping[str, Any]) -> dict[str, Any]:
    data = deepcopy(dict(event))
    # Never retain credentials or raw session blobs if a caller accidentally
    # stuffed them into notes/metadata.
    for key in ("password", "token", "clientSecretStr", "session"):
        data.pop(key, None)
    return data


def append_event(
    event: Mapping[str, Any],
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Append one journal event. Returns the public event that was written."""

    resolved = Path(path).expanduser() if path else default_history_path()
    payload = _public_event(event)
    if "event_id" not in payload:
        payload["event_id"] = new_event_id()
    if "recorded_at" not in payload:
        payload["recorded_at"] = utc_now()
    if "schema_version" not in payload:
        payload["schema_version"] = SCHEMA_VERSION
    outcome = str(payload.get("outcome") or "").strip()
    if outcome not in VALID_OUTCOMES:
        raise HistoryError(
            f"history outcome must be one of {sorted(VALID_OUTCOMES)}; got {outcome!r}"
        )
    source = str(payload.get("source") or SOURCE_LOCAL).strip() or SOURCE_LOCAL
    payload["source"] = source
    resolved.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    try:
        os.chmod(resolved, 0o600)
    except OSError:
        pass
    return payload


def load_events(path: str | Path | None = None) -> list[dict[str, Any]]:
    resolved = Path(path).expanduser() if path else default_history_path()
    if not resolved.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise HistoryError(f"history at {resolved} is unreadable") from exc
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HistoryError(f"history line {index} is not valid JSON") from exc
        if not isinstance(item, dict):
            raise HistoryError(f"history line {index} must be a JSON object")
        events.append(item)
    return events


def list_events(
    *,
    path: str | Path | None = None,
    limit: int = MAX_LIST_DEFAULT,
    source: str | None = None,
    outcome: str | None = None,
    query: str | None = None,
    recipe_sha256: str | None = None,
) -> list[dict[str, Any]]:
    if not 1 <= int(limit) <= MAX_LIST_HARD:
        raise HistoryError(f"history list limit must be 1-{MAX_LIST_HARD}")
    needle = (query or "").strip().casefold()
    wanted_source = (source or "").strip() or None
    wanted_outcome = (outcome or "").strip() or None
    wanted_sha = (recipe_sha256 or "").strip() or None
    selected: list[dict[str, Any]] = []
    for event in reversed(load_events(path)):
        if wanted_source and event.get("source") != wanted_source:
            continue
        if wanted_outcome and event.get("outcome") != wanted_outcome:
            continue
        if wanted_sha and event.get("recipe_sha256") != wanted_sha:
            continue
        if needle:
            haystack = " ".join(
                str(event.get(key, ""))
                for key in (
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
            ).casefold()
            if needle not in haystack:
                continue
        selected.append(_public_event(event))
        if len(selected) >= int(limit):
            break
    return selected


def history_summary(path: str | Path | None = None) -> dict[str, Any]:
    events = load_events(path)
    by_outcome: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for event in events:
        outcome = str(event.get("outcome") or "unknown")
        source = str(event.get("source") or "unknown")
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
    resolved = Path(path).expanduser() if path else default_history_path()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "total": len(events),
        "by_outcome": by_outcome,
        "by_source": by_source,
        "latest_recorded_at": events[-1].get("recorded_at") if events else None,
    }


def add_note(
    event_id: str,
    note: str,
    *,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Attach a tasting/operator note by appending a linked note event.

    The journal is append-only. Notes never rewrite earlier telemetry rows.
    """

    cleaned = _clean_text(note, field="note", max_chars=MAX_NOTE_CHARS)
    if not cleaned:
        raise HistoryError("note must be non-empty")
    target_id = str(event_id or "").strip()
    if not target_id:
        raise HistoryError("event_id is required")
    matches = [event for event in load_events(path) if event.get("event_id") == target_id]
    if not matches:
        raise HistoryError(f"history event {target_id!r} was not found")
    target = matches[-1]
    return append_event(
        {
            "outcome": target.get("outcome") or "imported",
            "source": SOURCE_LOCAL,
            "event_kind": "note",
            "related_event_id": target_id,
            "recipe_name": target.get("recipe_name"),
            "recipe_path": target.get("recipe_path"),
            "recipe_sha256": target.get("recipe_sha256"),
            "machine": target.get("machine"),
            "serving_kind": target.get("serving_kind"),
            "machine_program": target.get("machine_program"),
            "note": cleaned,
        },
        path=path,
    )


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
    """Build a local journal event from CLI workflow state and telemetry."""

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
    """Import normalised App brew-record dicts into the local journal."""

    existing = load_events(path)
    known_remote_ids = {
        str(event.get("remote_table_id"))
        for event in existing
        if event.get("source") == SOURCE_APP and event.get("remote_table_id") is not None
    }
    imported = 0
    skipped = 0
    written: list[dict[str, Any]] = []
    for raw in records:
        remote_id = raw.get("remote_table_id")
        if remote_id is not None and str(remote_id) in known_remote_ids:
            skipped += 1
            continue
        event = {
            "event_kind": "app_brew_record",
            "outcome": "imported",
            "source": SOURCE_APP,
            "region": region,
            "remote_table_id": remote_id,
            "recipe_name": raw.get("recipe_name"),
            "serving_kind": raw.get("serving_kind"),
            "machine_program": raw.get("machine_program"),
            "cup_type": raw.get("cup_type"),
            "dose_g": raw.get("dose_g"),
            "brew_time_s": raw.get("brew_time_s"),
            "create_time_stamp": raw.get("create_time_stamp"),
            "recorded_at": raw.get("recorded_at") or utc_now(),
            "has_line_chart": bool(raw.get("has_line_chart")),
            "is_pod": raw.get("is_pod"),
            "machine_id": raw.get("machine_id"),
            "member_used_recipes_id": raw.get("member_used_recipes_id"),
            "group_name": raw.get("group_name"),
            "recipe_sha256": raw.get("recipe_sha256"),
        }
        event = {key: value for key, value in event.items() if value not in (None, "", [], {})}
        written.append(append_event(event, path=path))
        if remote_id is not None:
            known_remote_ids.add(str(remote_id))
        imported += 1
    return {
        "imported": imported,
        "skipped_existing": skipped,
        "written_event_ids": [item["event_id"] for item in written],
    }


__all__ = [
    "HISTORY_PATH_ENV",
    "SOURCE_APP",
    "SOURCE_LOCAL",
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
    "utc_now",
]

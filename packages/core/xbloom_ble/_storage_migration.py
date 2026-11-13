"""Legacy JSON/JSONL backup, import, cutover reconciliation, and CLI entrypoints."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xbloom_paths import normalize_state_root, state_dir as resolve_state_dir
from ._storage_import_bridge import *  # noqa: F401,F403


def _storage_mod() -> Any:
    return sys.modules.get("xbloom_storage")


def _resolve_store_cls() -> type[Any]:
    mod = _storage_mod()
    cls = getattr(mod, "StateStore", None)
    if cls is not None:
        return cls
    from xbloom_storage import StateStore

    return StateStore


def _resolve_backup_fn() -> Any:
    mod = _storage_mod()
    return getattr(mod, "create_legacy_backup", create_legacy_backup)


def _maybe_fault(stage: str) -> None:
    mod = _storage_mod()
    hook = getattr(mod, "_migration_fault_hook", _migration_fault_hook)
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
    db = store if store is not None else _resolve_store_cls()(root)
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
        backup_manifest = _resolve_backup_fn()(
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
    store = _resolve_store_cls()(root)
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

    store = _resolve_store_cls()(state_root)
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
        store = _resolve_store_cls()(root)
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


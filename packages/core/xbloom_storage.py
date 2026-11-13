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


import xbloom_ble._storage_migration as _migration
import xbloom_ble._storage_primitives as _primitives
import xbloom_ble._storage_schema as _schema
from xbloom_ble._storage_catalog import StateStoreCatalogMixin
from xbloom_ble._storage_journal import StateStoreJournalMixin
from xbloom_ble._storage_workflow import StateStoreWorkflowMixin

for _mod in (_primitives, _schema, _migration):
    for _k, _v in vars(_mod).items():
        if not _k.startswith("__"):
            globals()[_k] = _v


class StateStore(
    StateStoreCatalogMixin,
    StateStoreWorkflowMixin,
    StateStoreJournalMixin,
):
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

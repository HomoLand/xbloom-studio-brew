"""Transactional SQLite storage for catalog, workflows, and idempotency.

Phase 0 baseline: schema, primitives, integrity, online backup, and a one-time
lossless import of legacy JSON/JSONL state. Physical BLE workflow semantics
remain in the bridge; this module does not rewrite them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from xbloom_paths import normalize_state_root, state_dir as resolve_state_dir

SCHEMA_VERSION = 2
DB_FILE_NAME = "state.db"
BUSY_TIMEOUT_MS = 5000
DEFAULT_BACKUP_DIRNAME = "backups"
LEGACY_MIGRATION_NAME = "legacy_json_v1"

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

# Injected failure hook for tests: callable taking stage name, may raise.
_migration_fault_hook: Any = None


class StorageError(RuntimeError):
    """Raised for storage, migration, or integrity failures."""


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


def default_db_path(state_root: Path | None = None) -> Path:
    root = normalize_state_root(state_root) if state_root is not None else resolve_state_dir()
    return root / DB_FILE_NAME


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


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
    "CREATE INDEX IF NOT EXISTS idx_recipe_revisions_recipe ON recipe_revisions(recipe_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_events_workflow ON workflow_events(workflow_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency(expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_legacy_imports_kind ON legacy_imports(source_kind)",
)

# Incremental migrations applied when opening a database created at an older
# SCHEMA_VERSION. Each entry is (target_version, name, statements).
# Version 1 was create-only baseline (SCHEMA_STATEMENTS). Version 2 adds
# active-workflow and idempotency-status indexes used by Phase A bridge APIs.
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

    def ensure_schema(self) -> int:
        """Create/migrate schema to SCHEMA_VERSION. Returns current version.

        Existing v1 databases are upgraded in place via SCHEMA_MIGRATIONS rather
        than assuming create-only. Each version is recorded in schema_migrations.
        """

        with self._init_lock:
            if self._initialized:
                row = self._connect().execute(
                    "SELECT MAX(version) AS v FROM schema_migrations"
                ).fetchone()
                return int(row["v"] or 0)

            conn = self._connect()
            # Always ensure base objects exist (IF NOT EXISTS is idempotent).
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)

            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            current = int(row["v"] or 0)

            # Fresh DB: no migration rows yet. Record baseline v1 first so
            # upgrades from older on-disk DBs and fresh opens share one path.
            if current == 0:
                checksum = sha256_text("\n".join(s.strip() for s in SCHEMA_STATEMENTS))
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO schema_migrations
                            (version, name, applied_at, checksum)
                        VALUES (?, ?, ?, ?)
                        """,
                        (1, "baseline_v1", utc_now(), checksum),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                current = 1

            if current > SCHEMA_VERSION:
                raise StorageError(
                    f"database schema version {current} is newer than "
                    f"supported {SCHEMA_VERSION}"
                )

            for target_version, name, statements in SCHEMA_MIGRATIONS:
                if current >= target_version:
                    continue
                if target_version != current + 1:
                    raise StorageError(
                        f"schema migration gap: at {current}, next is {target_version}"
                    )
                checksum = sha256_text("\n".join(s.strip() for s in statements))
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        """
                        INSERT INTO schema_migrations
                            (version, name, applied_at, checksum)
                        VALUES (?, ?, ?, ?)
                        """,
                        (target_version, name, utc_now(), checksum),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                current = target_version

            self._initialized = True
            return current

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
        data = _row_to_dict(row) or {}
        data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

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
        data = _row_to_dict(row) or {}
        data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

    def add_recipe_revision(
        self,
        recipe_id: str,
        content: Mapping[str, Any],
        *,
        revision_id: str | None = None,
        parent_revision_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        data = _row_to_dict(row) or {}
        data["content"] = json.loads(data.pop("content_json"))
        data["provenance"] = json.loads(data.pop("provenance_json") or "{}")
        return data

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
            ORDER BY updated_at DESC, created_at DESC
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
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """
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
            ORDER BY updated_at DESC, created_at DESC
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
        """Atomically finalize workflow state, append final event, complete idempotency.

        All three steps share one SQLite transaction. On any failure the whole
        commit rolls back so callers must not claim BLE release succeeded.

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
            # Preserve first terminal_at if already set (idempotent re-entry).
            terminal_at = row["terminal_at"] or now
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
            idem: dict[str, Any] | None = None
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

    stats = {"entries": 0, "recipes": 0, "revisions": 0, "skipped": 0}
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
            continue
        stats["entries"] += 1
        recipe_id = f"legacy_{entry_id}"
        kind = entry.get("kind")
        name = entry.get("name")
        conn.execute(
            """
            INSERT OR IGNORE INTO recipes (
                recipe_id, kind, name, created_at, updated_at,
                source, provenance_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_id,
                str(kind) if kind is not None else None,
                str(name) if name is not None else None,
                imported_at,
                imported_at,
                "legacy_catalog",
                canonical_json(
                    {
                        "legacy_entry_id": entry_id,
                        "sources": entry.get("sources"),
                        "origin": entry.get("origin"),
                    }
                ),
                canonical_json(
                    {
                        "executable": entry.get("executable"),
                        "slot_compatible": entry.get("slot_compatible"),
                    }
                ),
            ),
        )
        if conn.execute("SELECT changes()").fetchone()[0]:
            stats["recipes"] += 1
        recipe_body = entry.get("recipe")
        if isinstance(recipe_body, dict):
            rev_id = f"legacy_rev_{entry_id}_{content_sha256(recipe_body)[:16]}"
            content_json = canonical_json(recipe_body)
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at,
                        provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (
                        rev_id,
                        recipe_id,
                        content_json,
                        sha256_text(content_json),
                        imported_at,
                        canonical_json(
                            {
                                "source": "legacy_catalog",
                                "legacy_entry_id": entry_id,
                            }
                        ),
                    ),
                )
                stats["revisions"] += 1
            except sqlite3.IntegrityError:
                stats["skipped"] += 1
    return stats


def _import_history(
    store: StateStore,
    conn: sqlite3.Connection,
    path: Path,
    file_sha: str,
    imported_at: str,
) -> dict[str, int]:
    stats = {"events": 0, "skipped": 0, "lines": 0}
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
        if "event_id" not in event:
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
    return stats


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

    # Surface recovery as a workflow row so later Phase A can inspect it.
    workflow_kind = {
        "recovery_armed": "coffee_recovery",
        "recovery_tea": "tea_recovery",
        "recovery_grinder": "grinder_recovery",
    }.get(kind, kind)
    wid = f"legacy_{kind}_{file_sha[:16]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO workflows (
            workflow_id, kind, state, recipe_revision_id, snapshot_json,
            snapshot_sha256, source, owner, machine_phase, recovery_json,
            created_at, updated_at, terminal_at, metadata_json
        ) VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, ?, ?, ?, NULL, ?)
        """,
        (
            wid,
            workflow_kind,
            "recovery_imported",
            "legacy_migration",
            canonical_json(payload),
            imported_at,
            imported_at,
            canonical_json(
                {
                    "legacy_path": str(path),
                    "legacy_sha256": file_sha,
                    "source_kind": kind,
                }
            ),
        ),
    )
    return stats


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

    This does **not** switch runtime catalog/history writers to SQLite. While
    those remain JSON-backed, a completed migration receipt means the import
    snapshot is available in ``state.db`` -- not that SQLite is the active
    runtime source of truth.
    """

    root = normalize_state_root(state_root)
    # Close only stores we create. Caller-supplied ``store=`` (e.g. open_store)
    # stays open for the caller to manage -- critical on Windows where an open
    # SQLite handle blocks rename/delete of state.db until GC would free it.
    owns_store = store is None
    db = store if store is not None else StateStore(root)
    try:
        db.ensure_schema()

        if db.migration_completed() and not force:
            receipt = db.get_migration_receipt() or {}
            return {
                "status": "already_completed",
                "state_root": str(root),
                "receipt": receipt,
                "imported": False,
                "runtime_source_of_truth": "json_legacy",
                "message": (
                    "legacy import already completed; catalog/history runtime writers "
                    "remain JSON-backed until a later cutover"
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

                # Mark complete only inside the same transaction.
                conn.execute(
                    """
                    INSERT OR REPLACE INTO migration_receipts (
                        name, completed_at, backup_dir, manifest_json, stats_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        LEGACY_MIGRATION_NAME,
                        imported_at,
                        backup_manifest.get("backup_dir"),
                        canonical_json(backup_manifest),
                        canonical_json(stats),
                    ),
                )
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
            "runtime_source_of_truth": "json_legacy",
            "message": (
                "legacy JSON/JSONL imported into state.db; runtime catalog/history "
                "writers remain JSON-backed until a later cutover"
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
        completed = store.migration_completed()
        receipt = store.get_migration_receipt() if completed else None
        present = [
            {"kind": kind, "path": str(root / rel), "exists": (root / rel).is_file()}
            for kind, rel in LEGACY_SOURCES
        ]
        return {
            "state_root": str(root),
            "db_path": str(store.db_path),
            "migration_name": LEGACY_MIGRATION_NAME,
            "migration_completed": completed,
            "receipt": receipt,
            "legacy_sources": present,
            "runtime_source_of_truth": "json_legacy",
            "sqlite_active_runtime": False,
            "message": (
                "state.db holds schema, optional imported snapshots, and future workflow "
                "rows; catalog/history runtime writers are still JSON/JSONL-backed. "
                "Do not treat a completed migration receipt as SQLite cutover."
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
    must not pass ``migrate=True`` while runtime writers remain JSON-backed.

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
            "Explicit state.db maintenance. Migration is idempotent and does not "
            "switch runtime catalog/history writers to SQLite (still JSON-backed)."
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
                "runtime_source_of_truth": "json_legacy",
            }
        finally:
            store.close()
    else:  # pragma: no cover
        parser.error(f"unknown command {args.command}")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()

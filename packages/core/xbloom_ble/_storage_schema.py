"""SQLite schema DDL and incremental schema migrations for StateStore."""

from __future__ import annotations

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
    """
    CREATE TABLE IF NOT EXISTS beans (
        bean_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        origin TEXT,
        process TEXT,
        roast_level TEXT,
        altitude TEXT,
        flavor_notes TEXT,
        roast_date TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS preferences (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_beans_name ON beans(name)",
    "CREATE INDEX IF NOT EXISTS idx_beans_updated ON beans(updated_at)",
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
# Version 5 adds beans + preferences for dial-in / home opt-in (ROADMAP-NEXT Phase E).
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
    (
        5,
        "phase_e_beans_preferences_v5",
        (
            """
            CREATE TABLE IF NOT EXISTS beans (
                bean_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                origin TEXT,
                process TEXT,
                roast_level TEXT,
                altitude TEXT,
                flavor_notes TEXT,
                roast_date TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_beans_name ON beans(name)",
            "CREATE INDEX IF NOT EXISTS idx_beans_updated ON beans(updated_at)",
            """
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    ),
)

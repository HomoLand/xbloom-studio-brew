"""Phase 0 storage, migration, and SQLite concurrency tests (tmp state only)."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

import xbloom_storage as storage


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_schema_init_wal_and_version(tmp_path):
    store = storage.StateStore(tmp_path)
    version = store.ensure_schema()
    assert version == storage.SCHEMA_VERSION
    assert store.schema_version() == storage.SCHEMA_VERSION
    assert store.journal_mode() == "wal"
    names = {row["name"] for row in store.list_migrations()}
    assert "baseline_v1" in names
    assert "phase_0_history_events_journal_v4" in names
    tables = {
        row["name"]
        for row in store._connect().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "history_events" in tables
    check = store.integrity_check()
    assert check["ok"] is True
    store.close()


def test_recipe_workflow_event_idempotency_primitives(tmp_path):
    store = storage.StateStore(tmp_path)
    recipe = store.upsert_recipe(kind="coffee", name="Demo", source="test")
    rev = store.add_recipe_revision(
        recipe["recipe_id"],
        {"name": "Demo", "dose_g": 15},
        provenance={"knowledge": "fixture"},
    )
    assert rev["content_sha256"]
    loaded = store.get_recipe_revision(rev["revision_id"])
    assert loaded is not None
    assert loaded["content"]["dose_g"] == 15

    wf = store.create_workflow(
        kind="coffee",
        state="loaded",
        recipe_revision_id=rev["revision_id"],
        snapshot={"name": "Demo"},
        source="test",
    )
    event = store.append_workflow_event(
        wf["workflow_id"], "phase", {"phase": "loaded"}
    )
    assert event["seq"] == 1
    events = store.list_workflow_events(wf["workflow_id"])
    assert len(events) == 1

    first = store.put_idempotency(
        "req-1",
        "coffee.start",
        {"workflow_id": wf["workflow_id"]},
        result={"ok": True},
    )
    assert first["cached"] is False
    second = store.put_idempotency(
        "req-1",
        "coffee.start",
        {"workflow_id": wf["workflow_id"]},
        result={"ok": True},
    )
    assert second["cached"] is True
    assert second["result"] == {"ok": True}

    with pytest.raises(storage.StorageError, match="params hash"):
        store.put_idempotency(
            "req-1",
            "coffee.start",
            {"workflow_id": "other"},
            result={"ok": True},
        )
    store.close()


def test_online_backup(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.upsert_recipe(recipe_id="rcp_backup", name="B", kind="coffee")
    dest = tmp_path / "copy.db"
    out = store.backup(dest)
    assert out == dest
    assert dest.is_file()
    other = storage.StateStore(db_path=dest)
    other.ensure_schema()
    row = other._connect().execute(
        "SELECT name FROM recipes WHERE recipe_id = ?",
        ("rcp_backup",),
    ).fetchone()
    assert row["name"] == "B"
    store.close()
    other.close()


def test_legacy_migration_backup_hashes_and_idempotent(tmp_path):
    catalog = {
        "schema_version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "entries": [
            {
                "id": "entry-1",
                "name": "Hot Demo",
                "kind": "coffee",
                "recipe": {"name": "Hot Demo", "dose_g": 15, "grind": 50},
                "sources": [{"type": "fixture"}],
            }
        ],
    }
    history_line = {
        "event_id": "bh_test1",
        "outcome": "completed",
        "source": "local-skill",
        "schema_version": 1,
    }
    # One non-terminal recovery only (armed). Grinder rest is terminal cooldown.
    # Multiple non-terminal recoveries roll back (see multi-active test).
    armed = {"phase": "armed", "recipe_sha256": "abc"}
    grinder_rest = {
        "in_progress": False,
        "stopped_at": 1_700_000_000.0,
        "blocked_until": 1_700_000_060.0,
        "status": "rest",
    }
    _write(
        tmp_path / "catalog" / "catalog.json",
        json.dumps(catalog, indent=2),
    )
    _write(tmp_path / "brew-history.jsonl", json.dumps(history_line) + "\n")
    _write(tmp_path / "armed-state.json", json.dumps(armed))
    _write(tmp_path / "grinder-rest-state.json", json.dumps(grinder_rest))

    originals = {
        rel: (tmp_path / rel).read_bytes()
        for rel in (
            Path("catalog/catalog.json"),
            Path("brew-history.jsonl"),
            Path("armed-state.json"),
            Path("grinder-rest-state.json"),
        )
    }

    first = storage.migrate_legacy_state(tmp_path)
    assert first["status"] == "completed"
    assert first["imported"] is True
    assert first["history_cutover_completed"] is True
    assert first["catalog_cutover_completed"] is True
    backup_dir = Path(first["backup"]["backup_dir"])
    assert backup_dir.is_dir()
    manifest = json.loads((backup_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["file_count"] == 4
    for item in manifest["files"]:
        rel = Path(item["relative_path"])
        copied = backup_dir / rel
        assert copied.read_bytes() == originals[rel]
        assert storage.sha256_file(copied) == item["sha256"]
        # Originals untouched.
        assert (tmp_path / rel).read_bytes() == originals[rel]

    store = storage.StateStore(tmp_path)
    assert store.migration_completed(storage.LEGACY_MIGRATION_NAME)
    assert store.migration_completed(storage.LEGACY_HISTORY_CUTOVER_NAME)
    assert store.migration_completed(storage.LEGACY_CATALOG_CUTOVER_NAME)
    assert store.count_legacy_imports("catalog") >= 1
    assert store.count_legacy_imports("history") >= 1
    assert store.count_legacy_imports("recovery_armed") == 1
    assert store.count_legacy_imports("recovery_grinder") == 1
    grinder_sha = storage.sha256_bytes(originals[Path("grinder-rest-state.json")])
    grinder_wf = store.get_workflow(f"legacy_recovery_grinder_{grinder_sha[:16]}")
    assert grinder_wf is not None
    assert grinder_wf["kind"] == "grinder_recovery"
    assert grinder_wf["state"] == "cooldown_imported"
    assert grinder_wf["terminal_at"] is not None
    assert grinder_wf["recovery"]["blocked_until"] == grinder_rest["blocked_until"]
    assert grinder_wf["recovery"]["stopped_at"] == grinder_rest["stopped_at"]
    # History cutover: each JSONL line lands in history_events as well.
    assert store.count_history_events() == 1
    loaded = store.load_history_events()
    assert loaded[0]["event_id"] == "bh_test1"
    assert loaded[0]["outcome"] == "completed"
    # Catalog cutover: normalized entry is a full recipe envelope in SQLite.
    snap = store.build_catalog_snapshot(include_derived=False)
    assert len(snap["entries"]) == 1
    assert snap["entries"][0]["id"] == "entry-1"
    assert snap["source"] == "state.db"
    recipe = store.get_recipe(storage.recipe_id_for_catalog_entry_id("entry-1"))
    assert recipe is not None
    rev = store.get_latest_recipe_revision(recipe["recipe_id"])
    assert rev is not None
    assert rev["content"]["name"] == "Hot Demo"

    second = storage.migrate_legacy_state(tmp_path)
    assert second["status"] == "already_completed"
    assert second["imported"] is False
    assert second["history_cutover_completed"] is True
    assert second["catalog_cutover_completed"] is True
    # No duplicate catalog entries on rerun.
    assert store.count_legacy_imports("history") == 1
    assert store.count_history_events() == 1
    assert len(store.build_catalog_snapshot(include_derived=False)["entries"]) == 1
    store.close()


def test_migration_rollback_on_malformed_and_injected_fault(tmp_path, monkeypatch):
    _write(
        tmp_path / "brew-history.jsonl",
        "{not-json\n",
    )
    with pytest.raises(storage.StorageError, match="malformed history"):
        storage.migrate_legacy_state(tmp_path)
    store = storage.StateStore(tmp_path)
    assert not store.migration_completed()
    assert store.count_legacy_imports() == 0
    store.close()

    # Valid history, inject fault mid-transaction.
    _write(
        tmp_path / "brew-history.jsonl",
        json.dumps({"event_id": "bh_ok", "outcome": "completed", "source": "local-skill"})
        + "\n",
    )

    def fault(stage: str) -> None:
        if stage == "before_commit":
            raise RuntimeError("injected failure")

    monkeypatch.setattr(storage, "_migration_fault_hook", fault)
    with pytest.raises(RuntimeError, match="injected failure"):
        storage.migrate_legacy_state(tmp_path)
    monkeypatch.setattr(storage, "_migration_fault_hook", None)

    store = storage.StateStore(tmp_path)
    assert not store.migration_completed()
    assert store.count_legacy_imports() == 0
    # Original still present and unmodified.
    assert "bh_ok" in (tmp_path / "brew-history.jsonl").read_text(encoding="utf-8")
    store.close()


def test_upsert_recipe_preserves_omitted_provenance_and_returns_stored(tmp_path):
    store = storage.StateStore(tmp_path)
    first = store.upsert_recipe(
        recipe_id="rcp_keep",
        kind="coffee",
        name="Keep",
        provenance={"source": "fixture"},
        metadata={"tags": ["a"]},
    )
    assert first["provenance"] == {"source": "fixture"}
    assert first["metadata"] == {"tags": ["a"]}

    second = store.upsert_recipe(
        recipe_id="rcp_keep",
        name="Keep Updated",
        # provenance/metadata omitted -- must not wipe to {}
    )
    assert second["name"] == "Keep Updated"
    assert second["kind"] == "coffee"
    assert second["provenance"] == {"source": "fixture"}
    assert second["metadata"] == {"tags": ["a"]}
    loaded = store.get_recipe("rcp_keep")
    assert loaded is not None
    assert loaded["provenance"] == {"source": "fixture"}
    assert loaded["name"] == "Keep Updated"
    store.close()


def test_history_duplicate_event_ids_both_survive(tmp_path):
    line_a = {
        "event_id": "dup-id",
        "outcome": "completed",
        "source": "local-skill",
        "payload": "first",
    }
    line_b = {
        "event_id": "dup-id",
        "outcome": "failed",
        "source": "local-skill",
        "payload": "second",
    }
    _write(
        tmp_path / "brew-history.jsonl",
        json.dumps(line_a) + "\n" + json.dumps(line_b) + "\n",
    )
    result = storage.migrate_legacy_state(tmp_path)
    assert result["status"] == "completed"
    store = storage.StateStore(tmp_path)
    assert store.count_legacy_imports("history") == 2
    rows = store._connect().execute(
        "SELECT payload_json FROM legacy_imports WHERE source_kind = 'history' "
        "ORDER BY id"
    ).fetchall()
    payloads = [json.loads(r["payload_json"]) for r in rows]
    assert {p["payload"] for p in payloads} == {"first", "second"}
    assert all(p["event_id"] == "dup-id" for p in payloads)
    # Runtime journal also preserves both lines as distinct rows.
    assert store.count_history_events() == 2
    hist = store.load_history_events()
    assert {e.get("payload") for e in hist} == {"first", "second"}
    assert all(e["event_id"] == "dup-id" for e in hist)
    # force re-run remains idempotent for history_events
    forced = storage.migrate_legacy_state(tmp_path, force=True)
    assert forced["status"] == "completed"
    assert store.count_history_events() == 2
    store.close()


def test_migration_imports_from_backup_not_mutated_original(tmp_path, monkeypatch):
    original = {
        "event_id": "bh_stable",
        "outcome": "completed",
        "source": "local-skill",
        "note": "from-backup",
    }
    path = tmp_path / "brew-history.jsonl"
    _write(path, json.dumps(original) + "\n")

    def mutate_after_backup(stage: str) -> None:
        if stage == "after_backup":
            path.write_text(
                json.dumps(
                    {
                        "event_id": "bh_mutated",
                        "outcome": "failed",
                        "source": "local-skill",
                        "note": "mutated-live",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(storage, "_migration_fault_hook", mutate_after_backup)
    result = storage.migrate_legacy_state(tmp_path)
    monkeypatch.setattr(storage, "_migration_fault_hook", None)
    assert result["status"] == "completed"
    assert result["stats"]["import_from"] == "backup_copies"

    store = storage.StateStore(tmp_path)
    row = store._connect().execute(
        "SELECT payload_json FROM legacy_imports WHERE source_kind = 'history'"
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["note"] == "from-backup"
    assert payload["event_id"] == "bh_stable"
    # Live original was mutated; import still used backup.
    assert "mutated-live" in path.read_text(encoding="utf-8")
    store.close()


def test_migration_source_race_uses_manifest_not_pre_backup_listing(
    tmp_path, monkeypatch
):
    """Files appearing only after backup starts must not produce mismatched stats.

    Inject creation of a second legacy file immediately before the real backup
    call so the completed manifest, files_seen, and DB rows stay in agreement.
    """

    history = {
        "event_id": "bh_race",
        "outcome": "completed",
        "source": "local-skill",
    }
    _write(tmp_path / "brew-history.jsonl", json.dumps(history) + "\n")
    real_backup = storage.create_legacy_backup
    calls = {"n": 0}

    def backup_with_race(state_root, *, backup_root=None):
        calls["n"] += 1
        # Appear on the live root just before the real backup enumerates files.
        _write(
            Path(state_root) / "armed-state.json",
            json.dumps({"phase": "armed", "race": True}),
        )
        return real_backup(state_root, backup_root=backup_root)

    monkeypatch.setattr(storage, "create_legacy_backup", backup_with_race)
    result = storage.migrate_legacy_state(tmp_path)
    monkeypatch.setattr(storage, "create_legacy_backup", real_backup)

    assert result["status"] == "completed"
    assert calls["n"] == 1
    manifest = result["backup"]
    manifest_kinds = sorted(item["kind"] for item in manifest["files"])
    assert manifest_kinds == ["history", "recovery_armed"]
    assert sorted(result["stats"]["files_seen"]) == ["history", "recovery_armed"]
    # Manifest file list == files_seen (order may follow LEGACY_SOURCES).
    assert set(result["stats"]["files_seen"]) == {
        item["kind"] for item in manifest["files"]
    }

    store = storage.StateStore(tmp_path)
    assert store.count_legacy_imports("history") == 1
    assert store.count_legacy_imports("recovery_armed") == 1
    # No phantom kinds with empty import but listed in files_seen.
    assert store.count_legacy_imports("catalog") == 0
    store.close()


def test_multi_active_legacy_recovery_migration_rolls_back_losslessly(tmp_path):
    """Two non-terminal recoveries abort the whole import; originals stay intact."""

    armed = {"address": "AA:BB", "status": "armed"}
    tea = {"address": "CC:DD", "status": "tea_loaded"}
    coffee_path = tmp_path / "armed-state.json"
    tea_path = tmp_path / "tea-loaded-state.json"
    coffee_path.write_text(json.dumps(armed), encoding="utf-8")
    tea_path.write_text(json.dumps(tea), encoding="utf-8")
    coffee_bytes = coffee_path.read_bytes()
    tea_bytes = tea_path.read_bytes()
    coffee_sha = storage.sha256_bytes(coffee_bytes)
    tea_sha = storage.sha256_bytes(tea_bytes)

    with pytest.raises(storage.StorageError, match="more than one non-terminal recovery"):
        storage.migrate_legacy_state(tmp_path)

    # Originals remain byte-identical.
    assert coffee_path.read_bytes() == coffee_bytes
    assert tea_path.read_bytes() == tea_bytes

    store = storage.StateStore(tmp_path)
    try:
        assert not store.migration_completed(storage.LEGACY_MIGRATION_NAME)
        assert store.count_legacy_imports() == 0
        assert store.count_legacy_imports("recovery_armed") == 0
        assert store.count_legacy_imports("recovery_tea") == 0
        assert store.get_active_workflow() is None
        assert store.list_active_workflows() == []
        receipts = store._connect().execute(
            "SELECT name FROM migration_receipts"
        ).fetchall()
        assert receipts == []
    finally:
        store.close()

    # Backup directory and MANIFEST exist with byte-identical copies + matching SHA-256.
    backup_root = tmp_path / "backups"
    assert backup_root.is_dir()
    backups = sorted(backup_root.glob("legacy-*"))
    assert len(backups) == 1
    backup_dir = backups[0]
    assert backup_dir.is_dir()
    manifest_path = backup_dir / "MANIFEST.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["file_count"] == 2
    by_rel = {item["relative_path"]: item for item in manifest["files"]}
    assert set(by_rel) == {"armed-state.json", "tea-loaded-state.json"}
    coffee_copy = backup_dir / "armed-state.json"
    tea_copy = backup_dir / "tea-loaded-state.json"
    assert coffee_copy.read_bytes() == coffee_bytes
    assert tea_copy.read_bytes() == tea_bytes
    assert by_rel["armed-state.json"]["sha256"] == coffee_sha
    assert by_rel["tea-loaded-state.json"]["sha256"] == tea_sha
    assert storage.sha256_file(coffee_copy) == coffee_sha
    assert storage.sha256_file(tea_copy) == tea_sha


def test_get_latest_workflow_for_kinds_same_second_rowid_tiebreak(tmp_path):
    """Later-inserted same-second terminal grinder must win over an older row."""

    store = storage.StateStore(tmp_path)
    try:
        older = store.create_workflow(kind="grinder", state="running")
        store.commit_workflow_terminal(
            older["workflow_id"],
            state="stopped",
            event_payload={"result": "stopped", "marker": "older"},
        )
        newer = store.create_workflow(kind="grinder", state="running")
        store.commit_workflow_terminal(
            newer["workflow_id"],
            state="stopped",
            event_payload={"result": "stopped", "marker": "newer"},
        )
        fixed = "2026-07-23T12:00:00+00:00"
        conn = store._connect()
        conn.execute(
            """
            UPDATE workflows
            SET created_at = ?, updated_at = ?, terminal_at = ?
            WHERE kind = 'grinder'
            """,
            (fixed, fixed, fixed),
        )
        conn.commit()
        # Without rowid DESC, second-resolution timestamps make ordering unstable.
        stamp_rows = conn.execute(
            "SELECT workflow_id, updated_at, created_at, rowid FROM workflows "
            "WHERE kind = 'grinder' ORDER BY rowid ASC"
        ).fetchall()
        assert len(stamp_rows) == 2
        assert stamp_rows[0]["updated_at"] == stamp_rows[1]["updated_at"]
        assert stamp_rows[0]["created_at"] == stamp_rows[1]["created_at"]
        assert int(stamp_rows[0]["rowid"]) < int(stamp_rows[1]["rowid"])

        latest = store.get_latest_workflow_for_kinds(["grinder"], terminal=True)
        assert latest is not None
        assert latest["workflow_id"] == newer["workflow_id"]
        # Active non-terminal query is also deterministic under same stamps.
        a1 = store.create_workflow(kind="grinder", state="running")
        a2 = store.create_workflow(kind="grinder", state="running")
        conn.execute(
            "UPDATE workflows SET created_at = ?, updated_at = ? "
            "WHERE workflow_id IN (?, ?)",
            (fixed, fixed, a1["workflow_id"], a2["workflow_id"]),
        )
        conn.commit()
        active = store.get_latest_workflow_for_kinds(["grinder"], terminal=False)
        assert active is not None
        assert active["workflow_id"] == a2["workflow_id"]
    finally:
        store.close()


def test_grinder_recovery_import_classifies_terminal_and_active(tmp_path):
    """Stopped/rest grinder JSON -> cooldown_imported; in_progress/reserve -> recovery."""

    # Terminal cooldown import.
    stopped = {
        "in_progress": False,
        "stopped_at": 1_700_000_000.0,
        "blocked_until": 1_700_000_060.0,
        "owner": "legacy",
    }
    path = tmp_path / "grinder-rest-state.json"
    path.write_text(json.dumps(stopped), encoding="utf-8")
    original = path.read_bytes()
    result = storage.migrate_legacy_state(tmp_path)
    assert result["status"] == "completed"
    assert path.read_bytes() == original
    sha = storage.sha256_bytes(original)
    store = storage.StateStore(tmp_path)
    try:
        wf = store.get_workflow(f"legacy_recovery_grinder_{sha[:16]}")
        assert wf is not None
        assert wf["kind"] == "grinder_recovery"
        assert wf["state"] == "cooldown_imported"
        assert wf["terminal_at"] is not None
        assert wf["recovery"]["blocked_until"] == stopped["blocked_until"]
        assert store.get_active_workflow() is None
        latest = store.get_latest_workflow_for_kinds(
            ["grinder", "grinder_recovery"], terminal=True
        )
        assert latest is not None
        assert latest["workflow_id"] == wf["workflow_id"]
    finally:
        store.close()

    # Fresh root: active in_progress hydrates non-terminal recovery.
    root2 = tmp_path / "active"
    root2.mkdir()
    active = {"in_progress": True, "started_at": 1.0, "owner": "legacy"}
    (root2 / "grinder-rest-state.json").write_text(json.dumps(active), encoding="utf-8")
    storage.migrate_legacy_state(root2)
    store2 = storage.StateStore(root2)
    try:
        act = store2.get_active_workflow()
        assert act is not None
        assert act["kind"] == "grinder_recovery"
        assert act["state"] == "recovery_imported"
        assert act["terminal_at"] is None
        assert act["recovery"]["in_progress"] is True
    finally:
        store2.close()

    # Reserve-without-stop is also non-terminal.
    root3 = tmp_path / "reserve"
    root3.mkdir()
    reserved = {
        "reserved_at": 1.0,
        "runtime_s": 10.0,
        "blocked_until": 9_999_999_999.0,
    }
    (root3 / "grinder-rest-state.json").write_text(
        json.dumps(reserved), encoding="utf-8"
    )
    storage.migrate_legacy_state(root3)
    store3 = storage.StateStore(root3)
    try:
        act = store3.get_active_workflow()
        assert act is not None
        assert act["state"] == "recovery_imported"
        assert act["recovery"]["reserved_at"] == 1.0
    finally:
        store3.close()


def test_get_latest_workflow_for_kinds_validates_kinds(tmp_path):
    store = storage.StateStore(tmp_path)
    try:
        store.create_workflow(kind="grinder", state="running")
        store.create_workflow(kind="water", state="running")
        g = store.get_latest_workflow_for_kinds(["grinder"], terminal=False)
        assert g is not None and g["kind"] == "grinder"
        with pytest.raises(storage.StorageError, match="unknown workflow kind"):
            store.get_latest_workflow_for_kinds(["not_a_kind"])
        with pytest.raises(storage.StorageError, match="non-empty"):
            store.get_latest_workflow_for_kinds([])
    finally:
        store.close()


def test_foreign_key_and_parent_recipe_constraints(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    recipe_a = store.upsert_recipe(recipe_id="rcp_a", kind="coffee", name="A")
    recipe_b = store.upsert_recipe(recipe_id="rcp_b", kind="coffee", name="B")
    rev_a = store.add_recipe_revision(
        recipe_a["recipe_id"], {"name": "A", "dose_g": 15}
    )
    rev_b = store.add_recipe_revision(
        recipe_b["recipe_id"], {"name": "B", "dose_g": 16}
    )

    # Parent from a different recipe is rejected before insert (FK cannot enforce).
    with pytest.raises(storage.StorageError, match="different recipe"):
        store.add_recipe_revision(
            recipe_a["recipe_id"],
            {"name": "A2", "dose_g": 17},
            parent_revision_id=rev_b["revision_id"],
        )

    # Dangling parent revision id.
    with pytest.raises(storage.StorageError, match="unknown parent_revision_id"):
        store.add_recipe_revision(
            recipe_a["recipe_id"],
            {"name": "A3"},
            parent_revision_id="rev_does_not_exist",
        )

    # Same-recipe parent is fine.
    child = store.add_recipe_revision(
        recipe_a["recipe_id"],
        {"name": "A-child", "dose_g": 18},
        parent_revision_id=rev_a["revision_id"],
    )
    assert child["parent_revision_id"] == rev_a["revision_id"]

    # Dangling recipe_revision_id on workflows.
    with pytest.raises(storage.StorageError, match="integrity|FOREIGN KEY"):
        store.create_workflow(
            kind="coffee",
            state="loaded",
            recipe_revision_id="rev_missing",
        )

    # Nullable recipe_revision_id remains allowed (recovery/non-recipe workflows).
    free = store.create_workflow(kind="recovery", state="armed", recipe_revision_id=None)
    assert free["recipe_revision_id"] is None

    # Valid FK path.
    wf = store.create_workflow(
        kind="coffee",
        state="loaded",
        recipe_revision_id=rev_a["revision_id"],
    )

    # Dangling workflow_id on idempotency.
    with pytest.raises(storage.StorageError, match="integrity|FOREIGN KEY"):
        store.put_idempotency(
            "req-missing-wf",
            "coffee.start",
            {},
            workflow_id="wf_does_not_exist",
        )

    # Valid / null workflow_id on idempotency.
    ok = store.put_idempotency(
        "req-ok",
        "coffee.start",
        {"workflow_id": wf["workflow_id"]},
        workflow_id=wf["workflow_id"],
    )
    assert ok["workflow_id"] == wf["workflow_id"]
    null_wf = store.put_idempotency("req-null-wf", "ping", {}, workflow_id=None)
    assert null_wf["workflow_id"] is None
    store.close()


def test_migration_status_declares_full_sqlite_runtime_truth(tmp_path):
    status = storage.migration_status(tmp_path)
    assert status["runtime_source_of_truth"]["history"] == "sqlite"
    assert status["runtime_source_of_truth"]["workflow"] == "sqlite"
    assert status["runtime_source_of_truth"]["idempotency"] == "sqlite"
    assert status["runtime_source_of_truth"]["catalog"] == "sqlite"
    assert status["sqlite_active_runtime"] is True
    assert status["catalog_runtime"] == "sqlite"
    assert status["migration_completed"] is False
    assert status["history_cutover_completed"] is False
    assert status["catalog_cutover_completed"] is False
    assert status["history_cutover_name"] == storage.LEGACY_HISTORY_CUTOVER_NAME
    assert status["catalog_cutover_name"] == storage.LEGACY_CATALOG_CUTOVER_NAME

    _write(
        tmp_path / "brew-history.jsonl",
        json.dumps(
            {"event_id": "bh_truth", "outcome": "completed", "source": "local-skill"}
        )
        + "\n",
    )
    result = storage.migrate_legacy_state(tmp_path)
    assert result["runtime_source_of_truth"]["history"] == "sqlite"
    assert result["runtime_source_of_truth"]["catalog"] == "sqlite"
    assert result["imported"] is True
    assert result["history_cutover_completed"] is True
    assert result["catalog_cutover_completed"] is True
    status2 = storage.migration_status(tmp_path)
    assert status2["migration_completed"] is True
    assert status2["history_cutover_completed"] is True
    assert status2["catalog_cutover_completed"] is True
    assert status2["history_cutover_receipt"] is not None
    assert status2["catalog_cutover_receipt"] is not None
    assert status2["runtime_source_of_truth"]["history"] == "sqlite"
    assert status2["catalog_runtime"] == "sqlite"
    assert "catalog" in status2["sqlite_active_for"]
    assert "workflow" in status2["sqlite_active_for"]
    store = storage.StateStore(tmp_path)
    assert store.count_history_events() == 1
    store.close()


def test_pre_v4_old_receipt_backfills_history_on_normal_migrate(tmp_path):
    """Schema-v3-era state with only legacy_json_v1 must cut over without --force.

    Constructs pre-v4 data: history rows live only in legacy_imports, legacy
    import receipt is present, history_events is empty, no cutover receipt.
    A normal migrate must backfill once and remain idempotent on rerun.
    """

    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    imported_at = "2026-01-01T00:00:00+00:00"
    lines = [
        {
            "event_id": "dup-id",
            "outcome": "completed",
            "source": "local-skill",
            "payload": "first",
        },
        {
            "event_id": "dup-id",
            "outcome": "failed",
            "source": "local-skill",
            "payload": "second",
        },
        {
            "event_id": "bh_unique",
            "outcome": "completed",
            "source": "local-skill",
            "recipe_name": "Old Import",
        },
    ]
    with store.transaction() as conn:
        for idx, event in enumerate(lines, start=1):
            body = storage.canonical_json(event)
            digest = storage.sha256_text(body)
            record_key = f"line:{idx}:{digest}"
            conn.execute(
                """
                INSERT INTO legacy_imports (
                    source_kind, source_path, source_sha256, record_key,
                    payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "history",
                    str(tmp_path / "brew-history.jsonl"),
                    "fake_file_sha",
                    record_key,
                    body,
                    imported_at,
                ),
            )
        conn.execute(
            """
            INSERT INTO migration_receipts (
                name, completed_at, backup_dir, manifest_json, stats_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                storage.LEGACY_MIGRATION_NAME,
                imported_at,
                None,
                storage.canonical_json({"kind": "pre_v4_fixture"}),
                storage.canonical_json({"history": {"events": 3}}),
            ),
        )
    assert store.migration_completed(storage.LEGACY_MIGRATION_NAME)
    assert not store.migration_completed(storage.LEGACY_HISTORY_CUTOVER_NAME)
    assert store.count_legacy_imports("history") == 3
    assert store.count_history_events() == 0
    store.close()

    # Leave a JSONL that must NOT be reread for the cutover path.
    _write(
        tmp_path / "brew-history.jsonl",
        json.dumps(
            {
                "event_id": "bh_must_not_import",
                "outcome": "failed",
                "source": "local-skill",
            }
        )
        + "\n",
    )

    first = storage.migrate_legacy_state(tmp_path)
    assert first["status"] == "completed"
    assert first["imported"] is False
    assert first.get("history_backfilled") is True
    assert first["history_cutover_completed"] is True
    assert first["stats"]["history_cutover"]["history_events"] == 3

    store = storage.StateStore(tmp_path)
    assert store.migration_completed(storage.LEGACY_MIGRATION_NAME)
    assert store.migration_completed(storage.LEGACY_HISTORY_CUTOVER_NAME)
    assert store.count_history_events() == 3
    hist = store.load_history_events()
    assert {e.get("payload") for e in hist if e.get("payload")} == {"first", "second"}
    assert sum(1 for e in hist if e["event_id"] == "dup-id") == 2
    assert any(e["event_id"] == "bh_unique" for e in hist)
    assert not any(e["event_id"] == "bh_must_not_import" for e in hist)

    status = storage.migration_status(tmp_path)
    assert status["migration_completed"] is True
    assert status["history_cutover_completed"] is True
    assert status["history_cutover_receipt"] is not None

    second = storage.migrate_legacy_state(tmp_path)
    assert second["status"] == "already_completed"
    assert second["imported"] is False
    assert store.count_history_events() == 3
    store.close()


def test_online_backup_avoids_existing_destination_collision(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    dest = tmp_path / "fixed.db"
    store.backup(dest)
    with pytest.raises(storage.StorageError, match="already exists"):
        store.backup(dest)
    store.close()


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        storage.canonical_json({"x": float("nan")})


def test_concurrent_db_writers(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(n: int) -> None:
        local = storage.StateStore(tmp_path)
        try:
            barrier.wait(timeout=5)
            for i in range(20):
                local.upsert_recipe(
                    recipe_id=f"rcp_{n}_{i}",
                    name=f"R{n}-{i}",
                    kind="coffee",
                )
        except BaseException as exc:  # noqa: BLE001 - collect for main thread
            errors.append(exc)
        finally:
            local.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    count = store._connect().execute("SELECT COUNT(*) AS n FROM recipes").fetchone()["n"]
    assert count == 80
    assert store.integrity_check()["ok"] is True
    store.close()


# ---------------------------------------------------------------------------
# StateStore ownership: migrate_legacy_state / migration_status must close
# internally-created stores (Windows holds open SQLite handles until close).
# Caller-supplied store= must stay open. Do not rely on gc.collect.
# ---------------------------------------------------------------------------


class _CloseTrackingStore(storage.StateStore):
    """StateStore subclass that records close() without changing behaviour."""

    instances: list["_CloseTrackingStore"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_calls = 0
        type(self).instances.append(self)

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def _install_close_tracker(monkeypatch) -> type[_CloseTrackingStore]:
    _CloseTrackingStore.instances = []
    monkeypatch.setattr(storage, "StateStore", _CloseTrackingStore)
    return _CloseTrackingStore


def _write_minimal_history(root: Path) -> None:
    _write(
        root / "brew-history.jsonl",
        json.dumps(
            {"event_id": "bh_own", "outcome": "completed", "source": "local-skill"}
        )
        + "\n",
    )


def test_migrate_closes_owned_store_on_success(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write_minimal_history(tmp_path)
    result = storage.migrate_legacy_state(tmp_path)
    assert result["status"] == "completed"
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migrate_closes_owned_store_on_already_completed(tmp_path, monkeypatch):
    _write_minimal_history(tmp_path)
    first = storage.migrate_legacy_state(tmp_path)
    assert first["status"] == "completed"

    Tracker = _install_close_tracker(monkeypatch)
    second = storage.migrate_legacy_state(tmp_path)
    assert second["status"] == "already_completed"
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migrate_closes_owned_store_on_malformed_import(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write(tmp_path / "brew-history.jsonl", "{not-json\n")
    with pytest.raises(storage.StorageError, match="malformed history"):
        storage.migrate_legacy_state(tmp_path)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migrate_closes_owned_store_on_injected_import_fault(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write_minimal_history(tmp_path)

    def fault(stage: str) -> None:
        if stage == "before_commit":
            raise RuntimeError("injected ownership fault")

    monkeypatch.setattr(storage, "_migration_fault_hook", fault)
    with pytest.raises(RuntimeError, match="injected ownership fault"):
        storage.migrate_legacy_state(tmp_path)
    monkeypatch.setattr(storage, "_migration_fault_hook", None)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migrate_closes_owned_store_on_backup_failure(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write_minimal_history(tmp_path)

    def boom(state_root, *, backup_root=None):
        raise storage.StorageError("backup failed for ownership test")

    monkeypatch.setattr(storage, "create_legacy_backup", boom)
    with pytest.raises(storage.StorageError, match="backup failed"):
        storage.migrate_legacy_state(tmp_path)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migrate_closes_owned_store_on_manifest_failure(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write_minimal_history(tmp_path)

    def bad_manifest(state_root, *, backup_root=None):
        return {
            "backup_dir": str(tmp_path / "backups" / "fake"),
            "files": [
                {
                    "kind": "history",
                    "relative_path": "brew-history.jsonl",
                    # missing sha256 triggers manifest validation error
                }
            ],
        }

    monkeypatch.setattr(storage, "create_legacy_backup", bad_manifest)
    with pytest.raises(storage.StorageError, match="missing sha256"):
        storage.migrate_legacy_state(tmp_path)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migrate_does_not_close_caller_supplied_store(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write_minimal_history(tmp_path)
    caller = storage.StateStore(tmp_path)
    caller.ensure_schema()
    # Internal construction should not happen when store= is supplied.
    assert len(Tracker.instances) == 1
    result = storage.migrate_legacy_state(tmp_path, store=caller)
    assert result["status"] == "completed"
    assert len(Tracker.instances) == 1
    assert caller.close_calls == 0
    # Caller can still use the store after migration.
    assert caller.migration_completed()
    caller.close()
    assert caller.close_calls == 1


def test_open_store_migrate_leaves_returned_store_open(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    _write_minimal_history(tmp_path)
    store = storage.open_store(tmp_path, migrate=True)
    assert isinstance(store, Tracker)
    # open_store creates one store and passes it to migrate; must not be closed.
    assert store.close_calls == 0
    assert store.migration_completed()
    store.close()
    assert store.close_calls == 1


def test_open_store_closes_store_on_migrate_failure(tmp_path, monkeypatch):
    """Caller never receives the store; open_store must close it on failure."""

    Tracker = _install_close_tracker(monkeypatch)
    _write(tmp_path / "brew-history.jsonl", "{not-json\n")
    with pytest.raises(storage.StorageError, match="malformed history"):
        storage.open_store(tmp_path, migrate=True)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_open_store_closes_store_on_ensure_schema_failure(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)

    def boom(self):
        raise storage.StorageError("schema failure for open_store ownership")

    monkeypatch.setattr(Tracker, "ensure_schema", boom)
    with pytest.raises(storage.StorageError, match="schema failure"):
        storage.open_store(tmp_path, migrate=False)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_main_backup_closes_store_on_success(tmp_path, monkeypatch, capsys):
    Tracker = _install_close_tracker(monkeypatch)
    storage.main(["--state-dir", str(tmp_path), "backup"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "backed_up"
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_main_backup_closes_store_on_failure(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)

    def boom(self, destination=None):
        raise storage.StorageError("backup failed for main ownership test")

    monkeypatch.setattr(Tracker, "backup", boom)
    with pytest.raises(storage.StorageError, match="backup failed"):
        storage.main(["--state-dir", str(tmp_path), "backup"])
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migration_status_closes_store_on_success(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    status = storage.migration_status(tmp_path)
    assert status["migration_completed"] is False
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1


def test_migration_status_closes_store_on_failure(tmp_path, monkeypatch):
    Tracker = _install_close_tracker(monkeypatch)
    real_ensure = storage.StateStore.ensure_schema

    def boom(self):
        raise storage.StorageError("schema failure for ownership test")

    monkeypatch.setattr(Tracker, "ensure_schema", boom)
    with pytest.raises(storage.StorageError, match="schema failure"):
        storage.migration_status(tmp_path)
    assert len(Tracker.instances) == 1
    assert Tracker.instances[0].close_calls == 1
    # Restore for cleanliness (monkeypatch will also undo).
    monkeypatch.setattr(Tracker, "ensure_schema", real_ensure)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SQLite file-lock behaviour")
def test_migrate_releases_state_db_for_immediate_rename_delete(tmp_path):
    """Owned store must release Windows locks without relying on GC."""

    _write_minimal_history(tmp_path)
    result = storage.migrate_legacy_state(tmp_path)
    assert result["status"] == "completed"
    db = tmp_path / "state.db"
    assert db.is_file()
    # Immediate rename/delete must not raise PermissionError.
    moved = tmp_path / "state.db.moved"
    db.rename(moved)
    assert moved.is_file()
    assert not db.exists()
    moved.unlink()
    assert not moved.exists()
    # Also release WAL sidecars if present (closed connection).
    for side in ("state.db-wal", "state.db-shm"):
        side_path = tmp_path / side
        if side_path.exists():
            side_path.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SQLite file-lock behaviour")
def test_migration_status_releases_state_db_for_immediate_rename(tmp_path):
    storage.migration_status(tmp_path)
    db = tmp_path / "state.db"
    assert db.is_file()
    moved = tmp_path / "state.db.moved"
    db.rename(moved)
    moved.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SQLite file-lock behaviour")
def test_open_store_migrate_failure_releases_state_db_for_immediate_rename(tmp_path):
    """Failed open_store(migrate=True) must close so Windows can rename/delete."""

    _write(tmp_path / "brew-history.jsonl", "{not-json\n")
    with pytest.raises(storage.StorageError, match="malformed history"):
        storage.open_store(tmp_path, migrate=True)
    db = tmp_path / "state.db"
    assert db.is_file()
    moved = tmp_path / "state.db.moved"
    db.rename(moved)
    assert moved.is_file()
    assert not db.exists()
    moved.unlink()
    assert not moved.exists()
    for side in ("state.db-wal", "state.db-shm"):
        side_path = tmp_path / side
        if side_path.exists():
            side_path.unlink()


# ---------------------------------------------------------------------------
# Phase A storage: schema migration v1->v2, idempotency reserve, terminal txn
# ---------------------------------------------------------------------------


def test_schema_migrates_from_v1_database(tmp_path):
    """Open a database that only has baseline_v1 and upgrade to SCHEMA_VERSION."""

    # Create a v1-shaped DB by applying only baseline tables + version 1 row.
    db_path = tmp_path / "state.db"
    conn = __import__("sqlite3").connect(str(db_path))
    for statement in storage.SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
        "VALUES (1, 'baseline_v1', ?, 'legacy')",
        (storage.utc_now(),),
    )
    conn.commit()
    conn.close()

    store = storage.StateStore(tmp_path)
    version = store.ensure_schema()
    assert version == storage.SCHEMA_VERSION
    assert storage.SCHEMA_VERSION >= 2
    names = {row["name"] for row in store.list_migrations()}
    assert "baseline_v1" in names
    assert any("v2" in name or name.endswith("_v2") or "phase_a" in name for name in names)
    # Indexes from v2 migration exist.
    rows = store._connect().execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    index_names = {r[0] for r in rows}
    assert "idx_workflows_state_updated" in index_names
    assert "idx_idempotency_status" in index_names
    assert store.integrity_check()["ok"] is True
    store.close()


def test_idempotency_reserve_complete_pending_no_retry(tmp_path):
    store = storage.StateStore(tmp_path)
    wf = store.create_workflow(kind="coffee", state="loaded")
    first = store.reserve_idempotency(
        "req-a",
        "coffee.start",
        {"workflow_id": wf["workflow_id"]},
        workflow_id=wf["workflow_id"],
    )
    assert first["reserved"] is True
    assert first["status"] == storage.IDEM_PENDING

    pending = store.reserve_idempotency(
        "req-a",
        "coffee.start",
        {"workflow_id": wf["workflow_id"]},
        workflow_id=wf["workflow_id"],
    )
    assert pending.get("recovery_required") is True
    assert pending["status"] == storage.IDEM_PENDING

    completed = store.complete_idempotency("req-a", {"status": "running"})
    assert completed["status"] == storage.IDEM_COMPLETED
    cached = store.reserve_idempotency(
        "req-a",
        "coffee.start",
        {"workflow_id": wf["workflow_id"]},
        workflow_id=wf["workflow_id"],
    )
    assert cached["cached"] is True
    assert cached["result"] == {"status": "running"}

    with pytest.raises(storage.StorageError, match="method mismatch"):
        store.reserve_idempotency(
            "req-a",
            "coffee.pause",
            {"workflow_id": wf["workflow_id"]},
            workflow_id=wf["workflow_id"],
        )
    with pytest.raises(storage.StorageError, match="params hash"):
        store.reserve_idempotency(
            "req-a",
            "coffee.start",
            {"workflow_id": "other"},
            workflow_id=wf["workflow_id"],
        )
    with pytest.raises(storage.StorageError, match="workflow_id mismatch"):
        store.reserve_idempotency(
            "req-a",
            "coffee.start",
            {"workflow_id": wf["workflow_id"]},
            workflow_id="wf_other",
        )
    store.close()


def test_commit_workflow_terminal_transaction_and_rollback(tmp_path, monkeypatch):
    store = storage.StateStore(tmp_path)
    wf = store.create_workflow(kind="coffee", state="running")
    store.append_workflow_event(wf["workflow_id"], "started", {"ok": True})
    store.reserve_idempotency(
        "req-term",
        "cancel",
        {"workflow_id": wf["workflow_id"]},
        workflow_id=wf["workflow_id"],
    )

    result = store.commit_workflow_terminal(
        wf["workflow_id"],
        state="cancel_sent",
        event_type="terminal",
        event_payload={"result": "cancel_sent"},
        request_id="req-term",
        idempotency_result={"status": "cancel_sent"},
    )
    assert result["state"] == "cancel_sent"
    assert result["event"]["seq"] == 2
    loaded = store.get_workflow(wf["workflow_id"])
    assert loaded["terminal_at"] is not None
    idem = store.get_idempotency("req-term")
    assert idem["status"] == storage.IDEM_COMPLETED

    # Rollback: inject failure mid-transaction leaves prior terminal intact
    # and does not partially apply a second terminal event.
    wf2 = store.create_workflow(kind="tea", state="running")
    store.reserve_idempotency(
        "req-term-2",
        "cancel",
        {"workflow_id": wf2["workflow_id"]},
        workflow_id=wf2["workflow_id"],
    )
    real_complete = store.complete_idempotency

    def boom_complete(request_id, result, *, conn=None):
        raise storage.StorageError("injected complete failure")

    monkeypatch.setattr(store, "complete_idempotency", boom_complete)
    with pytest.raises(storage.StorageError, match="injected complete failure"):
        store.commit_workflow_terminal(
            wf2["workflow_id"],
            state="cancel_sent",
            event_type="terminal",
            event_payload={"result": "cancel_sent"},
            request_id="req-term-2",
            idempotency_result={"status": "cancel_sent"},
        )
    monkeypatch.setattr(store, "complete_idempotency", real_complete)

    rolled = store.get_workflow(wf2["workflow_id"])
    assert rolled["terminal_at"] is None
    assert rolled["state"] == "running"
    events = store.list_workflow_events(wf2["workflow_id"])
    assert events == []
    pending = store.get_idempotency("req-term-2")
    assert pending["status"] == storage.IDEM_PENDING
    store.close()


def test_active_and_latest_workflow_queries(tmp_path):
    store = storage.StateStore(tmp_path)
    a = store.create_workflow(kind="coffee", state="loaded")
    b = store.create_workflow(kind="scale", state="running")
    store.commit_workflow_terminal(a["workflow_id"], state="complete")
    active = store.get_active_workflow()
    assert active is not None
    assert active["workflow_id"] == b["workflow_id"]
    latest = store.get_latest_workflow()
    # latest may be a (just updated terminal) or b depending on timestamps;
    # both are valid "most recently updated" -- ensure it returns something.
    assert latest is not None
    assert latest["workflow_id"] in {a["workflow_id"], b["workflow_id"]}
    store.close()


def test_clear_recovery_sentinel_and_terminal_default(tmp_path):
    store = storage.StateStore(tmp_path)
    wf = store.create_workflow(
        kind="coffee",
        state="running",
        recovery={"reason": "control_unconfirmed"},
    )
    loaded = store.get_workflow(wf["workflow_id"])
    assert loaded["recovery"]["reason"] == "control_unconfirmed"
    # None preserves recovery.
    store.update_workflow(wf["workflow_id"], state="running", recovery=None)
    assert store.get_workflow(wf["workflow_id"])["recovery"]["reason"] == (
        "control_unconfirmed"
    )
    # CLEAR_RECOVERY clears.
    store.update_workflow(
        wf["workflow_id"], recovery=storage.CLEAR_RECOVERY
    )
    assert store.get_workflow(wf["workflow_id"])["recovery"] is None
    # Re-set and terminal default clears.
    store.update_workflow(
        wf["workflow_id"], recovery={"reason": "again"}
    )
    store.commit_workflow_terminal(wf["workflow_id"], state="complete")
    term = store.get_workflow(wf["workflow_id"])
    assert term["terminal_at"] is not None
    assert term["recovery"] is None
    store.close()


def test_create_workflow_with_event_atomic_and_transition(tmp_path, monkeypatch):
    store = storage.StateStore(tmp_path)
    created = store.create_workflow_with_event(
        kind="coffee",
        state="loading",
        snapshot={"name": "Demo"},
        event_type="created",
    )
    assert created["workflow_id"]
    events = store.list_workflow_events(created["workflow_id"])
    assert len(events) == 1
    assert events[0]["event_type"] == "created"

    transitioned = store.transition_workflow(
        created["workflow_id"],
        state="loaded",
        event_type="loaded",
        event_payload={"status": "armed"},
    )
    assert transitioned["state"] == "loaded"
    events = store.list_workflow_events(created["workflow_id"])
    assert [e["event_type"] for e in events] == ["created", "loaded"]

    # Rollback: if event append fails, state update must not stick.
    real_append = store.append_workflow_event_in_tx

    def boom_append(conn, workflow_id, event_type, payload=None):
        raise storage.StorageError("injected event append failure")

    monkeypatch.setattr(store, "append_workflow_event_in_tx", boom_append)
    with pytest.raises(storage.StorageError, match="injected event append"):
        store.transition_workflow(
            created["workflow_id"],
            state="starting",
            event_type="starting",
        )
    monkeypatch.setattr(store, "append_workflow_event_in_tx", real_append)
    rolled = store.get_workflow(created["workflow_id"])
    assert rolled["state"] == "loaded"
    assert len(store.list_workflow_events(created["workflow_id"])) == 2

    # create_workflow_with_event rolls back the row if event fails.
    def boom_append2(conn, workflow_id, event_type, payload=None):
        raise storage.StorageError("injected create event failure")

    monkeypatch.setattr(store, "append_workflow_event_in_tx", boom_append2)
    before = store.list_active_workflows()
    with pytest.raises(storage.StorageError, match="injected create event"):
        store.create_workflow_with_event(kind="tea", state="loading")
    monkeypatch.setattr(store, "append_workflow_event_in_tx", real_append)
    after = store.list_active_workflows()
    assert len(after) == len(before)
    store.close()

# ---------------------------------------------------------------------------
# Phase B B8: typed recipe/revision store (schema v3, OCC, provenance)
# ---------------------------------------------------------------------------

_COFFEE_CONTENT = {
    "name": "B8 Hot",
    "kind": "hot",
    "dripper": "Omni Dripper 2",
    "dose_g": 15,
    "grind": 58,
    "ratio": 16,
    "water_ml": 240,
    "hot_water_ml": 240,
    "pours": [
        {
            "label": "Bloom",
            "ml": 45,
            "temp_c": 92,
            "pattern": "spiral",
            "vibration": "after",
            "pause_s": 35,
            "rpm": 90,
            "flow_ml_s": 3.0,
        },
        {
            "label": "Main",
            "ml": 105,
            "temp_c": 92,
            "pattern": "spiral",
            "vibration": "none",
            "pause_s": 10,
            "rpm": 90,
            "flow_ml_s": 3.2,
        },
        {
            "label": "Finish",
            "ml": 90,
            "temp_c": 91,
            "pattern": "circular",
            "vibration": "none",
            "pause_s": 0,
            "rpm": 90,
            "flow_ml_s": 3.2,
        },
    ],
}

_TEA_CONTENT = {
    "name": "B8 Green",
    "kind": "tea",
    "leaf_g": 4,
    "output_ml_per_steep": 120,
    "pours": [
        {
            "label": "Steep 1",
            "ml": 90,
            "temp_c": 85,
            "pattern": "circular",
            "pause_s": 20,
            "flow_ml_s": 3.5,
        },
        {
            "label": "Steep 2",
            "ml": 90,
            "temp_c": 85,
            "pattern": "center",
            "pause_s": 15,
            "flow_ml_s": 3.5,
        },
    ],
}


def _coffee_edit(**overrides):
    data = json.loads(json.dumps(_COFFEE_CONTENT))
    data.update(overrides)
    return data


def test_schema_fresh_is_v5_with_archive_history_beans(tmp_path):
    store = storage.StateStore(tmp_path)
    version = store.ensure_schema()
    assert version == 5
    assert storage.SCHEMA_VERSION == 5
    names = {row["name"] for row in store.list_migrations()}
    assert "baseline_v1" in names
    assert any("v2" in n for n in names)
    assert any("v3" in n for n in names)
    assert any("v4" in n for n in names)
    assert any("v5" in n for n in names)
    cols = {
        r[1]
        for r in store._connect().execute("PRAGMA table_info(recipes)").fetchall()
    }
    assert "archived_at" in cols
    index_names = {
        r[0]
        for r in store._connect()
        .execute("SELECT name FROM sqlite_master WHERE type='index'")
        .fetchall()
    }
    assert "idx_recipes_updated_at" in index_names
    assert "idx_recipes_kind_updated" in index_names
    assert "idx_recipes_archived_updated" in index_names
    assert "idx_recipe_revisions_recipe_number" in index_names
    assert "idx_history_events_recorded" in index_names
    assert "idx_beans_name" in index_names
    tables = {
        r[0]
        for r in store._connect()
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert "history_events" in tables
    assert "beans" in tables
    assert "preferences" in tables
    # Fresh open must not re-apply migrations (single row per version).
    store.close()
    store2 = storage.StateStore(tmp_path)
    assert store2.ensure_schema() == 5
    v3_rows = store2._connect().execute(
        "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = 3"
    ).fetchone()
    v4_rows = store2._connect().execute(
        "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = 4"
    ).fetchone()
    v5_rows = store2._connect().execute(
        "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = 5"
    ).fetchone()
    assert v3_rows["n"] == 1
    assert v4_rows["n"] == 1
    assert v5_rows["n"] == 1
    assert store2.integrity_check()["ok"] is True
    store2.close()


def test_schema_migrates_v2_to_current(tmp_path):
    """v2-shaped DB (baseline + phase A indexes) upgrades through v3/v4 once."""

    db_path = tmp_path / "state.db"
    conn = __import__("sqlite3").connect(str(db_path))
    # v2 CREATE shape: no history_events table, no archived_at.
    v2_statements = [
        s
        for s in storage.SCHEMA_STATEMENTS
        if "history_events" not in s and "idx_history_events" not in s
    ]
    for statement in v2_statements:
        conn.execute(statement)
    for _target, name, statements in storage.SCHEMA_MIGRATIONS:
        if _target > 2:
            break
        for statement in statements:
            conn.execute(statement)
    now = storage.utc_now()
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
        "VALUES (1, 'baseline_v1', ?, 'x')",
        (now,),
    )
    conn.execute(
        "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
        "VALUES (2, 'phase_a_workflow_idempotency_indexes_v2', ?, 'y')",
        (now,),
    )
    # Seed a recipe without archived_at column (v2 shape).
    conn.execute(
        "INSERT INTO recipes (recipe_id, kind, name, created_at, updated_at, "
        "source, provenance_json, metadata_json) "
        "VALUES ('rcp_v2', 'coffee', 'Legacy', ?, ?, 'test', '{}', '{}')",
        (now, now),
    )
    conn.commit()
    conn.close()

    store = storage.StateStore(tmp_path)
    assert store.ensure_schema() == storage.SCHEMA_VERSION
    recipe = store.get_recipe("rcp_v2")
    assert recipe is not None
    assert recipe["name"] == "Legacy"
    assert recipe.get("archived_at") is None
    cols = {
        r[1]
        for r in store._connect().execute("PRAGMA table_info(recipes)").fetchall()
    }
    assert "archived_at" in cols
    tables = {
        r[0]
        for r in store._connect()
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert "history_events" in tables
    store.close()


def test_canonicalize_coffee_and_tea_and_reject_unsafe(tmp_path):
    coffee, kind_c = storage.canonicalize_recipe_content(_COFFEE_CONTENT)
    assert kind_c == "coffee"
    assert coffee["dose_g"] == 15
    assert coffee["name"] == "B8 Hot"
    # Stable hash for identical canonical content.
    again, _ = storage.canonicalize_recipe_content(coffee)
    assert storage.content_sha256(coffee) == storage.content_sha256(again)

    tea, kind_t = storage.canonicalize_recipe_content(_TEA_CONTENT)
    assert kind_t == "tea"
    assert tea["kind"] == "tea"
    assert tea["leaf_g"] == 4.0

    with pytest.raises(storage.StorageError, match="file path|mapping"):
        storage.canonicalize_recipe_content("/tmp/recipe.yaml")
    with pytest.raises(storage.StorageError, match="file path|mapping"):
        storage.canonicalize_recipe_content(Path("C:/recipes/demo.yaml"))
    with pytest.raises(storage.StorageError, match="invalid coffee"):
        storage.canonicalize_recipe_content(
            {"name": "bad", "dose_g": 15, "grind": 50, "pours": []}
        )
    with pytest.raises(storage.StorageError, match="invalid tea"):
        storage.canonicalize_recipe_content(
            {
                "name": "bad tea",
                "kind": "tea",
                "leaf_g": 99,
                "output_ml_per_steep": 120,
                "pours": [
                    {"ml": 90, "temp_c": 85, "pattern": "circular", "pause_s": 20}
                ],
            }
        )
    # Unsafe coffee (strict_validate): dose too low.
    with pytest.raises(storage.StorageError, match="invalid coffee"):
        storage.canonicalize_recipe_content(
            {
                "name": "tiny",
                "dose_g": 2,
                "grind": 55,
                "pours": [
                    {
                        "ml": 40,
                        "temp_c": 92,
                        "pattern": "spiral",
                        "pause_s": 30,
                        "rpm": 100,
                        "flow_ml_s": 3.0,
                    },
                    {
                        "ml": 200,
                        "temp_c": 92,
                        "pattern": "spiral",
                        "pause_s": 5,
                        "rpm": 100,
                        "flow_ml_s": 3.0,
                    },
                ],
            }
        )


def test_create_edit_list_get_canonical_coffee_tea(tmp_path):
    store = storage.StateStore(tmp_path)
    created = store.create_recipe_with_revision(
        _COFFEE_CONTENT,
        source="test",
        provenance={
            "knowledge_version": "1.0.0",
            "knowledge_hash": "abc",
            "provider": "openai-compatible",
            "model": "grok-4.5",
            "prompt_template_version": "pt-1",
            "schema_version": "rs-1",
            "candidate_hash": "cand1",
        },
        creation_source="unit-test",
        metadata={"tags": ["b8"]},
    )
    recipe = created["recipe"]
    rev = created["revision"]
    assert recipe["kind"] == "coffee"
    assert recipe["name"] == "B8 Hot"
    assert recipe["archived_at"] is None
    assert recipe["metadata"] == {"tags": ["b8"]}
    assert rev["revision_number"] == 1
    assert rev["parent_revision_id"] is None
    assert rev["content"]["dose_g"] == 15
    assert rev["provenance"]["creation_source"] == "unit-test"
    assert rev["provenance"]["knowledge_version"] == "1.0.0"
    assert rev["content_sha256"] == storage.content_sha256(rev["content"])

    latest = store.get_latest_recipe_revision(recipe["recipe_id"])
    assert latest is not None
    assert latest["revision_id"] == rev["revision_id"]
    assert latest["content_sha256"] == rev["content_sha256"]

    tea = store.create_recipe_with_revision(_TEA_CONTENT, source="test")
    assert tea["recipe"]["kind"] == "tea"
    assert tea["revision"]["content"]["leaf_g"] == 4.0

    edited = store.create_recipe_revision(
        recipe["recipe_id"],
        _coffee_edit(name="B8 Hot v2", note="tweaked"),
        expected_parent_revision_id=rev["revision_id"],
        provenance={"model": "grok-4.5", "candidate_hash": "cand2"},
        source="editor",
    )
    assert edited["revision"]["revision_number"] == 2
    assert edited["revision"]["parent_revision_id"] == rev["revision_id"]
    assert edited["recipe"]["name"] == "B8 Hot v2"
    assert edited["recipe"]["source"] == "editor"
    assert edited["revision"]["provenance"]["parent_revision_id"] == rev["revision_id"]
    # Content hash is stable for same canonical payload.
    same_again, _ = storage.canonicalize_recipe_content(edited["revision"]["content"])
    assert storage.content_sha256(same_again) == edited["revision"]["content_sha256"]

    revs = store.list_recipe_revisions(recipe["recipe_id"])
    assert [r["revision_number"] for r in revs] == [1, 2]
    assert revs[0]["revision_id"] == rev["revision_id"]
    assert revs[1]["revision_id"] == edited["revision"]["revision_id"]

    listed = store.list_recipes(kind="coffee")
    assert len(listed) == 1
    assert listed[0]["latest_revision"]["revision_number"] == 2
    assert listed[0]["latest_revision"]["content"]["name"] == "B8 Hot v2"

    teas = store.list_recipes(kind="tea", query="Green")
    assert len(teas) == 1
    assert teas[0]["kind"] == "tea"

    empty = store.list_recipes(query="does-not-match-zzz")
    assert empty == []
    with pytest.raises(storage.StorageError, match="limit"):
        store.list_recipes(limit=0)
    with pytest.raises(storage.StorageError, match="offset"):
        store.list_recipes(offset=-1)

    # Reject unsafe before opening write (no partial rows).
    before_count = store._connect().execute(
        "SELECT COUNT(*) AS n FROM recipes"
    ).fetchone()["n"]
    with pytest.raises(storage.StorageError, match="invalid coffee|file path"):
        store.create_recipe_with_revision("C:/local/path.yaml")
    with pytest.raises(storage.StorageError, match="invalid coffee"):
        store.create_recipe_revision(
            recipe["recipe_id"],
            {"name": "x", "dose_g": 15, "grind": 50, "pours": []},
            expected_parent_revision_id=edited["revision"]["revision_id"],
        )
    after_count = store._connect().execute(
        "SELECT COUNT(*) AS n FROM recipes"
    ).fetchone()["n"]
    assert after_count == before_count
    assert store.get_latest_recipe_revision(recipe["recipe_id"])["revision_number"] == 2
    store.close()


def test_forbidden_provenance_rejected_not_stripped(tmp_path):
    store = storage.StateStore(tmp_path)
    with pytest.raises(storage.StorageError, match="forbidden provenance"):
        store.create_recipe_with_revision(
            _COFFEE_CONTENT,
            provenance={"image_base64": "AAAA", "model": "x"},
        )
    with pytest.raises(storage.StorageError, match="forbidden provenance"):
        store.create_recipe_with_revision(
            _COFFEE_CONTENT,
            provenance={"nested": {"api_key": "sk-secret"}},
        )
    with pytest.raises(storage.StorageError, match="forbidden provenance"):
        store.create_recipe_with_revision(
            _COFFEE_CONTENT,
            provenance={"reasoning": "step by step..."},
        )
    with pytest.raises(storage.StorageError, match="forbidden provenance"):
        store.create_recipe_with_revision(
            _COFFEE_CONTENT,
            provenance={"local_path": "/home/user/bean.jpg"},
        )
    # Nested semantic keys that must be rejected recursively.
    for payload in (
        {"wrap": {"raw_image": "AAAA"}},
        {"wrap": {"session_token": "sess"}},
        {"wrap": {"source_path": "/tmp/bean.jpg"}},
        {"wrap": {"raw_reasoning": "step by step"}},
        {"API-Key": "sk"},
        {"sessionToken": "sess"},
        {"Raw Image": "x"},
        {"sourcePath": "/tmp/x"},
        {"rawReasoning": "think"},
        {"payload": b"\x00\x01raw"},
        {"nested": {"blob": bytearray(b"abc")}},
        {"items": [{"buf": memoryview(b"xyz")}]},
    ):
        with pytest.raises(storage.StorageError, match="forbidden"):
            store.create_recipe_with_revision(_COFFEE_CONTENT, provenance=payload)

    # Nothing stored on rejection.
    assert store.list_recipes() == []

    # Allowed ordinary keys plus non-substring false positives.
    # Forged parent_revision_id on first revision is omitted (not preserved).
    allowed = store.create_recipe_with_revision(
        _COFFEE_CONTENT,
        provenance={
            "knowledge_version": "1.0.0",
            "knowledge_hash": "abc",
            "provider": "openai-compatible",
            "model": "grok-4.5",
            "prompt_template_version": "pt-1",
            "schema_version": "rs-1",
            "candidate_hash": "cand1",
            "creation_source": "unit-test",
            "parent_revision_id": "rev_parent",
            "evidence": ["note-a"],
            "source_url": "https://example.com/recipe",
            "tokenizer": "cl100k_base",
            "pathway": "default",
        },
    )
    prov = allowed["revision"]["provenance"]
    assert prov["tokenizer"] == "cl100k_base"
    assert prov["pathway"] == "default"
    assert prov["source_url"] == "https://example.com/recipe"
    assert "parent_revision_id" not in prov
    assert allowed["revision"]["parent_revision_id"] is None

    # Low-level path still accepts extensible provenance for legacy/bridge.
    low = store.upsert_recipe(
        recipe_id="rcp_legacy",
        kind="coffee",
        name="Legacy",
        provenance={
            "import_note": "from catalog.json",
            "source": "legacy",
            "raw_image": "still-allowed-low-level",
            "session_token": "legacy-ok",
        },
    )
    assert low["provenance"]["import_note"] == "from catalog.json"
    assert low["provenance"]["raw_image"] == "still-allowed-low-level"
    store.close()


def test_provenance_safe_image_metadata_accepted_raw_rejected(tmp_path):
    """B8/B9: boolean image-use metadata is allowed; raw image material is not."""

    store = storage.StateStore(tmp_path)

    # Safe design-service metadata (boolean facts, not material).
    created = store.create_recipe_with_revision(
        _COFFEE_CONTENT,
        provenance={
            "used_image": True,
            "image_present": False,
            "model": "grok-4.5",
            "candidate_hash": "cand-img",
            "tokenizer": "cl100k_base",
            "pathway": "default",
        },
        creation_source="web-design",
    )
    prov = created["revision"]["provenance"]
    assert prov["used_image"] is True
    assert prov["image_present"] is False
    assert prov["model"] == "grok-4.5"
    assert prov["tokenizer"] == "cl100k_base"
    assert prov["pathway"] == "default"

    # Direct image/photo fields and raw material forms remain rejected.
    for payload in (
        {"image": "x"},
        {"images": ["a"]},
        {"photo": "x"},
        {"raw_image": "AAAA"},
        {"image_base64": "AAAA"},
        {"image_bytes": "AAAA"},
        {"image_data": "AAAA"},
        {"image_payload": "AAAA"},
        {"image_path": "/tmp/bean.jpg"},
        {"imageBase64": "AAAA"},
        {"imageData": "AAAA"},
        {"imagePayload": "AAAA"},
        {"imagePath": "/tmp/x"},
        {"rawImage": "x"},
        {"wrap": {"image_base64": "AAAA"}},
        {"used_image": {"nested": "not-scalar"}},
        {"image_present": ["not", "scalar"]},
        {"used_image": "data:image/png;base64,AAAA"},
        {"image_present": 1},
        {"blob": b"\x00\x01"},
    ):
        with pytest.raises(storage.StorageError, match="forbidden"):
            store.create_recipe_with_revision(_COFFEE_CONTENT, provenance=payload)

    # Only the one successful create above was stored.
    assert len(store.list_recipes()) == 1
    store.close()


def test_provenance_trusted_lineage_not_spoofable(tmp_path):
    """parent_revision_id and creation_source must follow trusted store values."""

    store = storage.StateStore(tmp_path)

    # First revision: forged parent is dropped; method creation_source wins.
    first = store.create_recipe_with_revision(
        _COFFEE_CONTENT,
        provenance={
            "parent_revision_id": "rev_forged_parent",
            "creation_source": "caller-spoof",
            "model": "grok-4.5",
        },
        creation_source="trusted-create",
    )
    first_rev = first["revision"]
    first_prov = first_rev["provenance"]
    assert first_rev["parent_revision_id"] is None
    assert "parent_revision_id" not in first_prov
    assert first_prov["creation_source"] == "trusted-create"
    assert first_prov["model"] == "grok-4.5"

    # Edit: provenance parent is forced to the real expected parent even if
    # the caller supplies a conflicting value; method creation_source wins.
    edited = store.create_recipe_revision(
        first["recipe"]["recipe_id"],
        _coffee_edit(name="B8 Hot spoof-guard", note="lineage"),
        expected_parent_revision_id=first_rev["revision_id"],
        provenance={
            "parent_revision_id": "rev_totally_wrong",
            "creation_source": "caller-spoof-edit",
            "candidate_hash": "cand-edit",
        },
        creation_source="trusted-edit",
    )
    edit_rev = edited["revision"]
    edit_prov = edit_rev["provenance"]
    assert edit_rev["parent_revision_id"] == first_rev["revision_id"]
    assert edit_prov["parent_revision_id"] == first_rev["revision_id"]
    assert edit_prov["parent_revision_id"] != "rev_totally_wrong"
    assert edit_prov["creation_source"] == "trusted-edit"
    assert edit_prov["candidate_hash"] == "cand-edit"

    # Without an explicit creation_source method arg, provenance value is kept.
    edited2 = store.create_recipe_revision(
        first["recipe"]["recipe_id"],
        _coffee_edit(name="B8 Hot keep-src", note="keep"),
        expected_parent_revision_id=edit_rev["revision_id"],
        provenance={
            "creation_source": "from-provenance-only",
            "parent_revision_id": "still-forged",
        },
    )
    assert edited2["revision"]["provenance"]["creation_source"] == "from-provenance-only"
    assert (
        edited2["revision"]["provenance"]["parent_revision_id"]
        == edit_rev["revision_id"]
    )
    store.close()


def test_provenance_classifier_direct_unit():
    """Semantic classifier: case/style variants, false positives, binary values."""

    storage.reject_forbidden_provenance(
        {
            "knowledge_version": "1",
            "tokenizer": "x",
            "pathway": "y",
            "used_image": True,
            "image_present": False,
            "evidence": [{"kind": "url", "source_url": "https://ex"}],
        }
    )
    for key in (
        "raw_image",
        "rawImage",
        "RAW-IMAGE",
        "image_base64",
        "image_data",
        "image_payload",
        "image_path",
        "image",
        "images",
        "photo",
        "session_token",
        "sessionToken",
        "source_path",
        "sourcePath",
        "raw_reasoning",
        "rawReasoning",
        "api_key",
        "api-key",
        "API Key",
    ):
        with pytest.raises(storage.StorageError, match="forbidden provenance"):
            storage.reject_forbidden_provenance({key: "x"})
    with pytest.raises(storage.StorageError, match="forbidden binary"):
        storage.reject_forbidden_provenance({"neutral": b"\x00"})
    with pytest.raises(storage.StorageError, match="forbidden binary"):
        storage.reject_forbidden_provenance({"n": bytearray(b"a")})
    with pytest.raises(storage.StorageError, match="forbidden binary"):
        storage.reject_forbidden_provenance({"n": memoryview(b"a")})
    with pytest.raises(storage.StorageError, match="forbidden non-boolean"):
        storage.reject_forbidden_provenance({"used_image": {"x": 1}})

    # Trusted lineage helpers.
    forced = storage.sanitize_recipe_provenance(
        {"parent_revision_id": "fake", "creation_source": "spoof"},
        parent_revision_id="rev_real",
        creation_source="trusted",
    )
    assert forced["parent_revision_id"] == "rev_real"
    assert forced["creation_source"] == "trusted"
    first = storage.sanitize_recipe_provenance(
        {"parent_revision_id": "fake", "model": "x"}
    )
    assert "parent_revision_id" not in first
    assert first["model"] == "x"


def test_concurrent_fresh_schema_init_two_independent_stores(tmp_path):
    """Two distinct StateStores racing ensure_schema on a fresh real SQLite DB.

    Repeated attempts catch intermittent database-is-locked races that a single
    barrier trial can miss. Both callers must return SCHEMA_VERSION with exactly
    one schema_migrations row per version.
    """

    attempts = 30
    for attempt in range(attempts):
        root = tmp_path / f"race_{attempt}"
        root.mkdir()
        barrier = threading.Barrier(2)
        results: list[object] = []
        lock = threading.Lock()

        def worker() -> None:
            store = storage.StateStore(root)
            try:
                barrier.wait(timeout=15)
                version = store.ensure_schema()
                with lock:
                    results.append(version)
            except Exception as exc:  # noqa: BLE001 - collect either outcome
                with lock:
                    results.append(exc)
            finally:
                store.close()

        t1 = threading.Thread(target=worker, name=f"schema-a-{attempt}")
        t2 = threading.Thread(target=worker, name=f"schema-b-{attempt}")
        t1.start()
        t2.start()
        t1.join(timeout=60)
        t2.join(timeout=60)
        assert not t1.is_alive() and not t2.is_alive(), attempt
        errors = [r for r in results if isinstance(r, BaseException)]
        versions = [r for r in results if not isinstance(r, BaseException)]
        assert not errors, (attempt, errors)
        assert versions == [storage.SCHEMA_VERSION, storage.SCHEMA_VERSION], (
            attempt,
            versions,
        )

        verify = storage.StateStore(root)
        assert verify.ensure_schema() == storage.SCHEMA_VERSION
        rows = verify.list_migrations()
        assert [row["version"] for row in rows] == list(
            range(1, storage.SCHEMA_VERSION + 1)
        )
        assert len(rows) == storage.SCHEMA_VERSION
        assert len({row["name"] for row in rows}) == storage.SCHEMA_VERSION
        for row in rows:
            assert row["checksum"]
        assert verify.integrity_check()["ok"] is True
        verify.close()


def test_archive_restore_with_revision_guard(tmp_path):
    store = storage.StateStore(tmp_path)
    created = store.create_recipe_with_revision(_COFFEE_CONTENT)
    rid = created["recipe"]["recipe_id"]
    rev1 = created["revision"]["revision_id"]

    archived = store.archive_recipe(rid, expected_latest_revision_id=rev1)
    assert archived["archived_at"] is not None
    assert store.list_recipes() == []
    listed = store.list_recipes(include_archived=True)
    assert len(listed) == 1
    assert listed[0]["archived_at"] is not None
    # Revisions and workflows still intact.
    assert len(store.list_recipe_revisions(rid)) == 1
    wf = store.create_workflow(
        kind="coffee",
        state="loaded",
        recipe_revision_id=rev1,
    )
    assert wf["recipe_revision_id"] == rev1

    restored = store.restore_recipe(rid, expected_latest_revision_id=rev1)
    assert restored["archived_at"] is None
    assert len(store.list_recipes()) == 1

    # Stale expected latest revision guard.
    edited = store.create_recipe_revision(
        rid,
        _coffee_edit(name="after restore"),
        expected_parent_revision_id=rev1,
    )
    with pytest.raises(storage.StorageConflictError, match="expected latest"):
        store.archive_recipe(rid, expected_latest_revision_id=rev1)
    store.archive_recipe(
        rid, expected_latest_revision_id=edited["revision"]["revision_id"]
    )
    assert store.get_recipe(rid)["archived_at"] is not None
    store.close()


def test_create_and_edit_rollback_on_injected_failure(tmp_path, monkeypatch):
    store = storage.StateStore(tmp_path)

    def fail_revision(stage):
        if stage == "before_revision_insert":
            raise storage.StorageError("injected revision insert failure")

    monkeypatch.setattr(storage, "_recipe_write_fault_hook", fail_revision)
    with pytest.raises(storage.StorageError, match="injected revision insert"):
        store.create_recipe_with_revision(_COFFEE_CONTENT, recipe_id="rcp_roll")
    monkeypatch.setattr(storage, "_recipe_write_fault_hook", None)
    assert store.get_recipe("rcp_roll") is None
    assert (
        store._connect()
        .execute("SELECT COUNT(*) AS n FROM recipe_revisions")
        .fetchone()["n"]
        == 0
    )

    created = store.create_recipe_with_revision(
        _COFFEE_CONTENT, recipe_id="rcp_edit", name="Original Name"
    )
    rev1 = created["revision"]["revision_id"]
    meta_before = created["recipe"]["provenance"]
    name_before = created["recipe"]["name"]

    def fail_update(stage):
        if stage == "before_recipe_update":
            raise storage.StorageError("injected recipe update failure")

    monkeypatch.setattr(storage, "_recipe_write_fault_hook", fail_update)
    with pytest.raises(storage.StorageError, match="injected recipe update"):
        store.create_recipe_revision(
            "rcp_edit",
            _coffee_edit(name="Should Not Stick"),
            expected_parent_revision_id=rev1,
            provenance={"model": "new-model"},
        )
    monkeypatch.setattr(storage, "_recipe_write_fault_hook", None)

    recipe = store.get_recipe("rcp_edit")
    assert recipe["name"] == name_before
    assert recipe["provenance"] == meta_before
    latest = store.get_latest_recipe_revision("rcp_edit")
    assert latest["revision_id"] == rev1
    assert latest["revision_number"] == 1
    assert len(store.list_recipe_revisions("rcp_edit")) == 1
    store.close()


def test_concurrent_expected_parent_conflict(tmp_path):
    store = storage.StateStore(tmp_path)
    created = store.create_recipe_with_revision(_COFFEE_CONTENT)
    rid = created["recipe"]["recipe_id"]
    parent = created["revision"]["revision_id"]
    # Second recipe proceeds under SQLite serialization while first is contested.
    other = store.create_recipe_with_revision(
        _tea_content_named("Other Tea"), recipe_id="rcp_other"
    )
    store.close()

    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        local = storage.StateStore(tmp_path)
        try:
            barrier.wait(timeout=10)
            out = local.create_recipe_revision(
                rid,
                _coffee_edit(name=f"from-{threading.current_thread().name}"),
                expected_parent_revision_id=parent,
            )
            with lock:
                results.append(out)
        except Exception as exc:  # noqa: BLE001 - collect either outcome
            with lock:
                results.append(exc)
        finally:
            local.close()

    t1 = threading.Thread(target=worker, name="w1")
    t2 = threading.Thread(target=worker, name="w2")
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    successes = [r for r in results if isinstance(r, dict)]
    conflicts = [r for r in results if isinstance(r, storage.StorageConflictError)]
    assert len(successes) == 1, results
    assert len(conflicts) == 1, results
    assert successes[0]["revision"]["revision_number"] == 2

    verify = storage.StateStore(tmp_path)
    revs = verify.list_recipe_revisions(rid)
    assert len(revs) == 2
    numbers = [r["revision_number"] for r in revs]
    assert numbers == [1, 2]
    # Different recipe still healthy.
    assert verify.get_latest_recipe_revision("rcp_other")["revision_id"] == (
        other["revision"]["revision_id"]
    )
    # Editing the other recipe still works after the conflict race.
    tea2 = verify.create_recipe_revision(
        "rcp_other",
        _tea_content_named("Other Tea 2"),
        expected_parent_revision_id=other["revision"]["revision_id"],
    )
    assert tea2["revision"]["revision_number"] == 2
    verify.close()


def _tea_content_named(name: str) -> dict:
    data = json.loads(json.dumps(_TEA_CONTENT))
    data["name"] = name
    return data


def _catalog_entry(entry_id="entry-a", name="Alpha Hot", dose=15, grind=50):
    return {
        "id": entry_id,
        "name": name,
        "kind": "coffee",
        "origin": "created",
        "executable": True,
        "slot_compatible": False,
        "sources": [{"type": "fixture", "file": "t.json"}],
        "recipe": {
            "name": name,
            "kind": "hot",
            "dose_g": dose,
            "grind": grind,
            "pours": [
                {
                    "ml": 45,
                    "temp_c": 92,
                    "pattern": "spiral",
                    "pause_s": 30,
                    "flow_ml_s": 3.0,
                    "vibration": "after",
                    "rpm": 90,
                },
                {
                    "ml": 180,
                    "temp_c": 92,
                    "pattern": "spiral",
                    "pause_s": 0,
                    "flow_ml_s": 3.2,
                    "vibration": "none",
                    "rpm": 90,
                },
            ],
        },
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
    }


def test_old_receipt_backfills_catalog_on_normal_migrate(tmp_path):
    """legacy_json_v1 only: catalog rows in legacy_imports, partial recipes.

    A normal migrate without --force must repair full catalog envelopes from
    legacy_imports and must not reread catalog.json.
    """

    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    imported_at = "2026-01-01T00:00:00+00:00"
    entry = _catalog_entry("xbloom:9001", name="Backfill Coffee")
    body = storage.canonical_json(entry)
    digest = storage.sha256_text(body)
    record_key = f"entry:xbloom:9001:{digest}"
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO legacy_imports (
                source_kind, source_path, source_sha256, record_key,
                payload_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "catalog",
                str(tmp_path / "catalog" / "catalog.json"),
                "fake_catalog_sha",
                record_key,
                body,
                imported_at,
            ),
        )
        # Partial v3-era recipe row (flags only, no full envelope).
        conn.execute(
            """
            INSERT INTO recipes (
                recipe_id, kind, name, created_at, updated_at,
                source, provenance_json, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy_xbloom:9001",
                "coffee",
                "Backfill Coffee",
                imported_at,
                imported_at,
                "legacy_catalog",
                storage.canonical_json({"legacy_entry_id": "xbloom:9001"}),
                storage.canonical_json({"executable": True, "slot_compatible": False}),
            ),
        )
        conn.execute(
            """
            INSERT INTO migration_receipts (
                name, completed_at, backup_dir, manifest_json, stats_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                storage.LEGACY_MIGRATION_NAME,
                imported_at,
                None,
                storage.canonical_json({"kind": "pre_catalog_cutover_fixture"}),
                storage.canonical_json({"catalog": {"entries": 1}}),
            ),
        )
        # History cutover already done so only catalog is pending.
        conn.execute(
            """
            INSERT INTO migration_receipts (
                name, completed_at, backup_dir, manifest_json, stats_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                storage.LEGACY_HISTORY_CUTOVER_NAME,
                imported_at,
                None,
                storage.canonical_json({"kind": "history_cutover"}),
                storage.canonical_json({}),
            ),
        )
    assert store.migration_completed(storage.LEGACY_MIGRATION_NAME)
    assert store.migration_completed(storage.LEGACY_HISTORY_CUTOVER_NAME)
    assert not store.migration_completed(storage.LEGACY_CATALOG_CUTOVER_NAME)
    store.close()

    # Live catalog.json that must NOT be reread for the cutover path.
    _write(
        tmp_path / "catalog" / "catalog.json",
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    _catalog_entry("must-not-import", name="Must Not Import")
                ],
            }
        ),
    )
    original_bytes = (tmp_path / "catalog" / "catalog.json").read_bytes()

    first = storage.migrate_legacy_state(tmp_path)
    assert first["status"] == "completed"
    assert first["imported"] is False
    assert first.get("catalog_backfilled") is True
    assert first["catalog_cutover_completed"] is True
    assert first["stats"]["catalog_cutover"]["source_rows"] == 1

    # Original JSON untouched.
    assert (tmp_path / "catalog" / "catalog.json").read_bytes() == original_bytes

    store = storage.StateStore(tmp_path)
    assert store.migration_completed(storage.LEGACY_CATALOG_CUTOVER_NAME)
    snap = store.build_catalog_snapshot(include_derived=False)
    assert len(snap["entries"]) == 1
    assert snap["entries"][0]["id"] == "xbloom:9001"
    assert snap["entries"][0]["name"] == "Backfill Coffee"
    assert "catalog_envelope" in (store.get_recipe("legacy_xbloom:9001") or {}).get(
        "metadata", {}
    )
    assert not any(e["id"] == "must-not-import" for e in snap["entries"])
    revs = store.list_recipe_revisions("legacy_xbloom:9001")
    assert len(revs) == 1

    second = storage.migrate_legacy_state(tmp_path)
    assert second["status"] == "already_completed"
    assert len(store.build_catalog_snapshot(include_derived=False)["entries"]) == 1
    assert len(store.list_recipe_revisions("legacy_xbloom:9001")) == 1
    store.close()


def test_catalog_merge_metadata_only_and_content_revision(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    entry = _catalog_entry()
    first = store.merge_catalog_entries([entry], source="test")
    assert first["created"] == 1
    rid = storage.recipe_id_for_catalog_entry_id("entry-a")
    rev1 = store.get_latest_recipe_revision(rid)
    assert rev1 is not None
    assert rev1["revision_number"] == 1

    # Metadata-only change: same recipe body, new source tag.
    meta_entry = dict(entry)
    meta_entry["sources"] = [
        {"type": "fixture", "file": "t.json"},
        {"type": "cloud", "endpoint": "created"},
    ]
    meta_entry["executable"] = False
    second = store.merge_catalog_entries([meta_entry], source="test")
    assert second["metadata_only"] == 1
    assert second["updated"] == 0
    rev2 = store.get_latest_recipe_revision(rid)
    assert rev2["revision_id"] == rev1["revision_id"]
    recipe = store.get_recipe(rid)
    assert recipe["metadata"]["executable"] is False
    assert len(recipe["metadata"]["catalog_envelope"]["sources"]) == 2

    # Content change creates one new child revision.
    changed = dict(entry)
    changed["recipe"] = dict(entry["recipe"])
    changed["recipe"]["dose_g"] = 16
    changed["name"] = "Alpha Hot 16"
    third = store.merge_catalog_entries([changed], source="test")
    assert third["updated"] == 1
    rev3 = store.get_latest_recipe_revision(rid)
    assert rev3["revision_number"] == 2
    assert rev3["parent_revision_id"] == rev1["revision_id"]
    assert rev3["content"]["dose_g"] == 16

    # Unchanged replay is truly idempotent: no UPDATE at all.
    before = store.get_recipe(rid)
    before_revs = store.list_recipe_revisions(rid)
    fourth = store.merge_catalog_entries([changed], source="test")
    assert fourth["unchanged"] == 1
    assert fourth["metadata_only"] == 0
    assert fourth["updated"] == 0
    after = store.get_recipe(rid)
    assert after["updated_at"] == before["updated_at"]
    assert after["source"] == before["source"]
    assert after["metadata"] == before["metadata"]
    assert after["provenance"] == before["provenance"]
    assert after["archived_at"] is None
    assert len(store.list_recipe_revisions(rid)) == len(before_revs) == 2
    store.close()


def test_catalog_merge_true_idempotency_byte_stable(tmp_path):
    """Exact same entry + source must leave the recipe row byte-stable."""

    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    entry = _catalog_entry("stable-1", name="Stable Hot")
    store.merge_catalog_entries([entry], source="test")
    rid = storage.recipe_id_for_catalog_entry_id("stable-1")
    before = store.get_recipe(rid)
    conn = store._connect()
    raw_before = conn.execute(
        "SELECT updated_at, source, metadata_json, provenance_json, archived_at "
        "FROM recipes WHERE recipe_id = ?",
        (rid,),
    ).fetchone()
    rev_count_before = conn.execute(
        "SELECT COUNT(*) AS n FROM recipe_revisions WHERE recipe_id = ?",
        (rid,),
    ).fetchone()["n"]

    stats = store.merge_catalog_entries([entry], source="test")
    assert stats == {
        "candidates": 1,
        "created": 0,
        "updated": 0,
        "metadata_only": 0,
        "unchanged": 1,
        "skipped": 0,
    }
    raw_after = conn.execute(
        "SELECT updated_at, source, metadata_json, provenance_json, archived_at "
        "FROM recipes WHERE recipe_id = ?",
        (rid,),
    ).fetchone()
    rev_count_after = conn.execute(
        "SELECT COUNT(*) AS n FROM recipe_revisions WHERE recipe_id = ?",
        (rid,),
    ).fetchone()["n"]
    assert tuple(raw_after) == tuple(raw_before)
    assert rev_count_after == rev_count_before == 1
    after = store.get_recipe(rid)
    assert after["updated_at"] == before["updated_at"]
    assert after["metadata"] == before["metadata"]
    assert after["provenance"] == before["provenance"]
    store.close()


def test_catalog_revision_id_collision_keeps_both_recipes_complete(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    entry = _catalog_entry("revision-collision", name="Catalog Recipe")
    body = entry["recipe"]
    deterministic_id = (
        "legacy_rev_revision-collision_"
        + storage.sha256_text(storage.canonical_json(body))[:16]
    )
    store.upsert_recipe(recipe_id="unrelated", kind="coffee", name="Unrelated")
    unrelated = store.add_recipe_revision(
        "unrelated",
        body,
        revision_id=deterministic_id,
    )

    stats = store.merge_catalog_entries([entry], source="test")
    assert stats["created"] == 1
    catalog_id = storage.recipe_id_for_catalog_entry_id("revision-collision")
    catalog_revisions = store.list_recipe_revisions(catalog_id)
    assert len(catalog_revisions) == 1
    assert catalog_revisions[0]["revision_id"] != deterministic_id
    assert catalog_revisions[0]["content"] == body
    assert store.get_recipe_revision(deterministic_id)["recipe_id"] == "unrelated"
    assert store.get_recipe_revision(deterministic_id)["revision_id"] == unrelated[
        "revision_id"
    ]
    store.close()


def test_catalog_restore_archived_is_metadata_only_when_content_unchanged(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    entry = _catalog_entry("restore-me")
    store.merge_catalog_entries([entry], source="test")
    rid = storage.recipe_id_for_catalog_entry_id("restore-me")
    store.archive_catalog_entry(entry_id="restore-me")
    archived = store.get_recipe(rid)
    assert archived["archived_at"] is not None
    revs_before = store.list_recipe_revisions(rid)

    stats = store.merge_catalog_entries([entry], source="test")
    assert stats["metadata_only"] == 1
    assert stats["updated"] == 0
    assert stats["unchanged"] == 0
    restored = store.get_recipe(rid)
    assert restored["archived_at"] is None
    assert len(store.list_recipe_revisions(rid)) == len(revs_before) == 1
    store.close()


def test_catalog_ownership_collision_refuses_unrelated_web_recipe(tmp_path):
    """deterministic legacy_<entry_id> must never take over an unrelated Web recipe."""

    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    collision_id = "collision"
    recipe_id = storage.recipe_id_for_catalog_entry_id(collision_id)
    assert recipe_id == "legacy_collision"
    created = store.create_recipe_with_revision(
        _coffee_edit(name="Web Collision Target"),
        recipe_id=recipe_id,
        source="web",
        creation_source="web-ui",
        name="Web Collision Target",
        provenance={"sources": [{"type": "web-state", "recipe_id": recipe_id}]},
        metadata={"user_note": "keep me"},
    )
    assert created["recipe"]["recipe_id"] == recipe_id
    web_before = store.get_recipe(recipe_id)
    revs_before = store.list_recipe_revisions(recipe_id)

    with pytest.raises(storage.StorageConflictError, match="without catalog ownership"):
        store.merge_catalog_entries(
            [_catalog_entry(collision_id, name="Hostile Catalog")],
            source="test",
        )

    web_after = store.get_recipe(recipe_id)
    assert web_after["name"] == "Web Collision Target"
    assert web_after["updated_at"] == web_before["updated_at"]
    assert web_after["metadata"] == web_before["metadata"]
    assert web_after["provenance"] == web_before["provenance"]
    assert web_after["source"] == "web"
    assert len(store.list_recipe_revisions(recipe_id)) == len(revs_before)
    # Not catalog-owned despite legacy_ prefix + provenance.sources.
    owned = store.build_catalog_snapshot(include_derived=False)
    assert owned["entries"] == []
    derived = store.build_catalog_snapshot(include_derived=True)
    hit = next(e for e in derived["entries"] if e["recipe_id"] == recipe_id)
    assert hit["catalog_owned"] is False
    assert hit["derived"] is True
    store.close()


def test_catalog_ownership_mismatched_marker_is_conflict(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    rid = storage.recipe_id_for_catalog_entry_id("target")
    store.create_recipe_with_revision(
        _coffee_edit(name="Wrong Owner"),
        recipe_id=rid,
        source="legacy_catalog",
        name="Wrong Owner",
        provenance={"legacy_entry_id": "other-entry"},
        metadata={"catalog_entry_id": "other-entry"},
    )
    with pytest.raises(storage.StorageConflictError, match="conflicts with existing ownership"):
        store.merge_catalog_entries([_catalog_entry("target")], source="test")
    store.close()


def test_legacy_prefix_alone_is_not_catalog_owned(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    created = store.create_recipe_with_revision(
        _coffee_edit(name="Prefix Only"),
        recipe_id="legacy_prefix_only",
        source="web",
        name="Prefix Only",
        provenance={"sources": [{"type": "annotation", "note": "not ownership"}]},
    )
    rid = created["recipe"]["recipe_id"]
    owned = store.build_catalog_snapshot(include_derived=False)
    assert all(e.get("recipe_id") != rid for e in owned["entries"])
    derived = store.build_catalog_snapshot(include_derived=True)
    hit = next(e for e in derived["entries"] if e["recipe_id"] == rid)
    assert hit["catalog_owned"] is False
    assert hit["derived"] is True
    store.close()


def test_catalog_envelope_strips_runtime_view_fields_and_preserves_annotations(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    entry = _catalog_entry("env-1", name="Envelope Clean")
    # Pollute incoming with snapshot runtime fields.
    polluted = {
        **entry,
        "recipe_id": "should-not-persist",
        "catalog_owned": True,
        "derived": False,
        "archived_at": "2099-01-01T00:00:00+00:00",
    }
    store.merge_catalog_entries([polluted], source="test")
    rid = storage.recipe_id_for_catalog_entry_id("env-1")
    # Inject unrelated Web/user annotations that must survive re-merge.
    conn = store._connect()
    recipe = store.get_recipe(rid)
    meta = dict(recipe["metadata"])
    meta["user_annotation"] = {"label": "keep"}
    prov = dict(recipe["provenance"])
    prov["browser_tag"] = "chrome"
    conn.execute(
        "UPDATE recipes SET metadata_json = ?, provenance_json = ? WHERE recipe_id = ?",
        (
            storage.canonical_json(meta),
            storage.canonical_json(prov),
            rid,
        ),
    )

    snap = store.build_catalog_snapshot(include_derived=False)
    loaded = next(e for e in snap["entries"] if e["id"] == "env-1")
    assert loaded["recipe_id"] == rid
    assert loaded["catalog_owned"] is True
    # Round-trip save of snapshot entry must not pollute envelope or churn.
    before = store.get_recipe(rid)
    stats = store.merge_catalog_entries([loaded], source="test")
    assert stats["unchanged"] == 1
    after = store.get_recipe(rid)
    envelope = after["metadata"]["catalog_envelope"]
    for key in ("recipe_id", "catalog_owned", "derived", "archived_at", "id"):
        assert key not in envelope
    assert after["metadata"]["user_annotation"] == {"label": "keep"}
    assert after["provenance"]["browser_tag"] == "chrome"
    assert after["updated_at"] == before["updated_at"]
    assert after["metadata"]["catalog_envelope"] == before["metadata"]["catalog_envelope"]
    store.close()


def test_catalog_archive_retains_revisions(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.merge_catalog_entries([_catalog_entry("del-me")], source="test")
    archived = store.archive_catalog_entry(entry_id="del-me")
    assert archived["archived"] is True
    active = store.build_catalog_snapshot(include_derived=False)
    assert active["entries"] == []
    all_entries = store.build_catalog_snapshot(
        include_derived=False, include_archived=True
    )
    assert len(all_entries["entries"]) == 1
    revs = store.list_recipe_revisions(storage.recipe_id_for_catalog_entry_id("del-me"))
    assert len(revs) == 1
    store.close()


def test_catalog_archive_exact_id_only_no_name_fallback(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.merge_catalog_entries(
        [_catalog_entry("exact-id", name="Unique Archive Name")],
        source="test",
    )
    # Name substring must not archive via this API.
    with pytest.raises(storage.StorageError, match="exact catalog-owned id"):
        store.archive_catalog_entry(entry_id="Unique Archive")
    with pytest.raises(storage.StorageError, match="exact catalog-owned id"):
        store.archive_catalog_entry(entry_id="exact")
    # Unrelated derived Web recipe must never archive through this API.
    web = store.create_recipe_with_revision(
        _coffee_edit(name="Web Only"),
        source="web",
        name="Web Only",
    )
    web_id = web["recipe"]["recipe_id"]
    with pytest.raises(storage.StorageError, match="not found for archive"):
        store.archive_catalog_entry(entry_id=web_id)
    assert store.get_recipe(web_id)["archived_at"] is None
    # Exact id still works.
    ok = store.archive_catalog_entry(entry_id="exact-id")
    assert ok["entry_id"] == "exact-id"
    store.close()


def test_catalog_archive_table_id_exact_and_ambiguous(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    a = _catalog_entry("tid-a", name="A")
    a["table_id"] = 42
    b = _catalog_entry("tid-b", name="B")
    b["table_id"] = 42
    store.merge_catalog_entries([a, b], source="test")
    with pytest.raises(storage.StorageError, match="ambiguous"):
        store.archive_catalog_entry(table_id=42)
    # Single exact mapping succeeds.
    store2 = storage.StateStore(tmp_path / "solo")
    store2.ensure_schema()
    solo = _catalog_entry("solo", name="Solo")
    solo["table_id"] = 99
    store2.merge_catalog_entries([solo], source="test")
    archived = store2.archive_catalog_entry(table_id=99)
    assert archived["entry_id"] == "solo"
    assert archived["table_id"] == 99
    with pytest.raises(storage.StorageError, match="not found for archive"):
        store2.archive_catalog_entry(table_id=1000)
    store.close()
    store2.close()


def test_concurrent_catalog_merge_disjoint_entries(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.close()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(prefix: str) -> None:
        local = storage.StateStore(tmp_path)
        try:
            barrier.wait(timeout=5)
            entries = [
                _catalog_entry(f"{prefix}-{i}", name=f"{prefix} {i}", dose=15 + i)
                for i in range(8)
            ]
            local.merge_catalog_entries(entries, source="concurrent")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            local.close()

    t1 = threading.Thread(target=worker, args=("left",))
    t2 = threading.Thread(target=worker, args=("right",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert errors == []
    store = storage.StateStore(tmp_path)
    snap = store.build_catalog_snapshot(include_derived=False)
    ids = {e["id"] for e in snap["entries"]}
    assert len(ids) == 16
    assert all(f"left-{i}" in ids for i in range(8))
    assert all(f"right-{i}" in ids for i in range(8))
    store.close()


def test_same_entry_content_race_is_deterministic(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.merge_catalog_entries([_catalog_entry("race-1")], source="seed")
    store.close()
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    entry = _catalog_entry("race-1", dose=17)

    def worker() -> None:
        local = storage.StateStore(tmp_path)
        try:
            barrier.wait(timeout=5)
            local.merge_catalog_entries([entry], source="race")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            local.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    store = storage.StateStore(tmp_path)
    rid = storage.recipe_id_for_catalog_entry_id("race-1")
    revs = store.list_recipe_revisions(rid)
    # Seed rev1 + at most one content change to dose 17.
    assert len(revs) == 2
    assert revs[-1]["content"]["dose_g"] == 17
    store.close()


def test_web_recipe_visible_in_skill_catalog_view(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    created = store.create_recipe_with_revision(
        _coffee_edit(name="Web Hot"),
        source="web",
        creation_source="web-ui",
        name="Web Hot",
    )
    recipe_id = created["recipe"]["recipe_id"]
    snap = store.build_catalog_snapshot(include_derived=True)
    ids = {e["id"] for e in snap["entries"]}
    assert recipe_id in ids
    derived = next(e for e in snap["entries"] if e["id"] == recipe_id)
    assert derived["derived"] is True
    assert derived["catalog_owned"] is False
    assert derived["name"] == "Web Hot"
    store.close()


def test_skill_imported_recipe_readable_by_web_apis(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    entry = _catalog_entry("skill-1", name="Skill Export")
    store.merge_catalog_entries([entry], source="skill")
    rid = storage.recipe_id_for_catalog_entry_id("skill-1")
    recipe = store.get_recipe(rid)
    assert recipe is not None
    assert recipe["name"] == "Skill Export"
    latest = store.get_latest_recipe_revision(rid)
    assert latest is not None
    assert latest["content"]["name"] == "Skill Export"
    assert latest["content"]["dose_g"] == 15
    listed = store.list_recipes(query="Skill")
    assert any(item["recipe_id"] == rid for item in listed)
    store.close()


def test_catalog_merge_fault_rolls_back(tmp_path, monkeypatch):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.merge_catalog_entries([_catalog_entry("keep")], source="seed")

    def fault(stage: str) -> None:
        if stage == "before_commit":
            raise RuntimeError("catalog fault")

    # Inject fault inside merge transaction via nested path: use migration hook
    # only for migrate path. For merge, raise mid-transaction by monkeypatching
    # _merge_one_catalog_entry_in_tx after first success.
    original = store._merge_one_catalog_entry_in_tx
    calls = {"n": 0}

    def flaky(conn, entry, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise storage.StorageError("injected merge failure")
        return original(conn, entry, **kwargs)

    monkeypatch.setattr(store, "_merge_one_catalog_entry_in_tx", flaky)
    with pytest.raises(storage.StorageError, match="injected merge failure"):
        store.merge_catalog_entries(
            [_catalog_entry("keep"), _catalog_entry("new-one")],
            source="fault",
        )
    # Transaction rolled back: new-one absent, keep still present.
    snap = store.build_catalog_snapshot(include_derived=False)
    ids = {e["id"] for e in snap["entries"]}
    assert "keep" in ids
    assert "new-one" not in ids
    store.close()


def test_no_catalog_json_write_after_cutover(tmp_path):
    import xbloom_catalog as catalog

    selector = tmp_path / "catalog" / "catalog.json"
    catalog.save_catalog(
        {**catalog.empty_catalog(), "entries": [_catalog_entry()]},
        selector,
    )
    assert not selector.exists()
    db = tmp_path / "state.db"
    assert db.is_file()
    loaded = catalog.load_catalog(selector)
    assert len(loaded["entries"]) == 1
    assert loaded["source"] == "state.db"
    assert "state.db" in str(loaded["path"])

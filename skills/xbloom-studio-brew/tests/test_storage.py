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
    armed = {"phase": "armed", "recipe_sha256": "abc"}
    _write(
        tmp_path / "catalog" / "catalog.json",
        json.dumps(catalog, indent=2),
    )
    _write(tmp_path / "brew-history.jsonl", json.dumps(history_line) + "\n")
    _write(tmp_path / "armed-state.json", json.dumps(armed))
    _write(tmp_path / "tea-loaded-state.json", json.dumps({"phase": "loaded"}))
    _write(tmp_path / "grinder-rest-state.json", json.dumps({"status": "rest"}))

    originals = {
        rel: (tmp_path / rel).read_bytes()
        for rel in (
            Path("catalog/catalog.json"),
            Path("brew-history.jsonl"),
            Path("armed-state.json"),
            Path("tea-loaded-state.json"),
            Path("grinder-rest-state.json"),
        )
    }

    first = storage.migrate_legacy_state(tmp_path)
    assert first["status"] == "completed"
    assert first["imported"] is True
    backup_dir = Path(first["backup"]["backup_dir"])
    assert backup_dir.is_dir()
    manifest = json.loads((backup_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["file_count"] == 5
    for item in manifest["files"]:
        rel = Path(item["relative_path"])
        copied = backup_dir / rel
        assert copied.read_bytes() == originals[rel]
        assert storage.sha256_file(copied) == item["sha256"]
        # Originals untouched.
        assert (tmp_path / rel).read_bytes() == originals[rel]

    store = storage.StateStore(tmp_path)
    assert store.migration_completed()
    assert store.count_legacy_imports("catalog") >= 1
    assert store.count_legacy_imports("history") >= 1
    assert store.count_legacy_imports("recovery_armed") == 1

    second = storage.migrate_legacy_state(tmp_path)
    assert second["status"] == "already_completed"
    assert second["imported"] is False
    # No duplicate catalog entries on rerun.
    assert store.count_legacy_imports("history") == 1
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
        # provenance/metadata omitted — must not wipe to {}
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


def test_migration_status_declares_json_runtime_truth(tmp_path):
    status = storage.migration_status(tmp_path)
    assert status["runtime_source_of_truth"] == "json_legacy"
    assert status["sqlite_active_runtime"] is False
    assert status["migration_completed"] is False

    # Completing migration does not flip runtime source of truth.
    _write(
        tmp_path / "brew-history.jsonl",
        json.dumps(
            {"event_id": "bh_truth", "outcome": "completed", "source": "local-skill"}
        )
        + "\n",
    )
    result = storage.migrate_legacy_state(tmp_path)
    assert result["runtime_source_of_truth"] == "json_legacy"
    assert result["imported"] is True
    status2 = storage.migration_status(tmp_path)
    assert status2["migration_completed"] is True
    assert status2["runtime_source_of_truth"] == "json_legacy"
    assert status2["sqlite_active_runtime"] is False


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

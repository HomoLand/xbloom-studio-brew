"""History facade and SQLite runtime journal tests."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import xbloom
import xbloom_history as history
import xbloom_storage as storage


def test_history_status_list_and_note_round_trip(monkeypatch, tmp_path, capsys):
    # Deprecated env selects state root (parent of legacy JSONL path).
    path = tmp_path / "brew-history.jsonl"
    monkeypatch.setenv(history.HISTORY_PATH_ENV, str(path))
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(xbloom, "reexec_in_local_runtime", lambda: None)

    event = history.append_event(
        history.event_from_workflow(
            command="start",
            outcome="completed",
            state={
                "machine": "XBLOOM",
                "recipe_path": str(tmp_path / "recipe.yaml"),
                "recipe_sha256": "abc123",
                "serving_kind": "hot",
                "target_dispensed_water_ml": 225,
            },
            summary={"name": "Test Hot", "dose_g": 15, "grind": 54},
            monitor={
                "completion_confirmed": True,
                "dispensed_water_ml": 224.5,
                "elapsed_s": 130.0,
            },
        ),
        path=path,
    )

    # Cutover: never write JSONL at runtime.
    assert not path.exists()
    db = tmp_path / "state.db"
    assert db.is_file()

    assert xbloom.main(["history", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["total"] == 1
    assert status["by_outcome"]["completed"] == 1
    assert status["source"] == "state.db"
    assert status["authoritative"] == "sqlite"
    assert "state.db" in status["path"]

    assert xbloom.main(["history", "list", "--limit", "5"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["events"][0]["event_id"] == event["event_id"]
    assert listed["source"] == "state.db"

    assert (
        xbloom.main(
            [
                "history",
                "note",
                event["event_id"],
                "brighter citrus, a bit thin",
            ]
        )
        == 0
    )
    noted = json.loads(capsys.readouterr().out)
    assert noted["status"] == "noted"
    assert noted["event"]["related_event_id"] == event["event_id"]
    assert noted["source"] == "state.db"

    events = history.load_events(path=path)
    assert len(events) == 2
    assert events[-1]["event_kind"] == "note"
    assert events[-1]["note"] == "brighter citrus, a bit thin"
    assert not path.exists()


def test_import_app_records_is_idempotent(tmp_path):
    path = tmp_path / "brew-history.jsonl"
    records = [
        {
            "remote_table_id": 9,
            "recipe_name": "Phone Brew",
            "serving_kind": "coffee",
            "dose_g": 15,
            "brew_time_s": 120,
            "create_time_stamp": 1784000000,
        }
    ]
    first = history.import_app_records(records, path=path, region="china")
    second = history.import_app_records(records, path=path, region="china")
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["skipped_existing"] == 1
    events = history.load_events(path=path)
    assert len(events) == 1
    assert events[0]["source"] == "app-cloud"
    assert not path.exists()


def test_append_event_id_retries_are_idempotent(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    event = {
        "event_id": "bh_retry",
        "outcome": "completed",
        "source": "local-skill",
        "recipe_name": "Once",
    }
    first = store.append_history_event(event)
    second = store.append_history_event(event)
    assert first["event_id"] == second["event_id"] == "bh_retry"
    assert store.count_history_events() == 1
    store.close()


def test_append_event_id_conflict_on_material_payload_change(tmp_path):
    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.append_history_event(
        {
            "event_id": "bh_conflict",
            "outcome": "completed",
            "source": "local-skill",
            "recipe_name": "First",
        }
    )
    with pytest.raises(storage.StorageConflictError, match="dedupe conflict"):
        store.append_history_event(
            {
                "event_id": "bh_conflict",
                "outcome": "completed",
                "source": "local-skill",
                "recipe_name": "Second",
            }
        )
    assert store.count_history_events() == 1
    assert store.load_history_events()[0]["recipe_name"] == "First"
    store.close()


def test_filters_summary_and_query(tmp_path):
    store = storage.StateStore(tmp_path)
    store.append_history_event(
        {
            "event_id": "bh_a",
            "outcome": "completed",
            "source": "local-skill",
            "recipe_name": "Ethiopia",
            "recipe_sha256": "sha_a",
            "note": "bright",
        }
    )
    store.append_history_event(
        {
            "event_id": "bh_b",
            "outcome": "failed",
            "source": "app-cloud",
            "recipe_name": "Colombia",
            "recipe_sha256": "sha_b",
        }
    )
    listed = store.list_history_events(limit=10, outcome="completed")
    assert len(listed) == 1
    assert listed[0]["event_id"] == "bh_a"
    by_source = store.list_history_events(limit=10, source="app-cloud")
    assert len(by_source) == 1
    by_sha = store.list_history_events(limit=10, recipe_sha256="sha_a")
    assert len(by_sha) == 1
    by_query = store.list_history_events(limit=10, query="ethi")
    assert len(by_query) == 1
    summary = store.history_summary()
    assert summary["total"] == 2
    assert summary["by_outcome"]["completed"] == 1
    assert summary["by_source"]["app-cloud"] == 1
    assert summary["source"] == "state.db"
    store.close()


def test_list_history_query_searches_full_journal_not_newest_window(tmp_path):
    """Query must match older rows even when >1000 newer rows exist."""

    store = storage.StateStore(tmp_path)
    store.ensure_schema()
    store.append_history_event(
        {
            "event_id": "bh_old_match",
            "outcome": "completed",
            "source": "local-skill",
            "recipe_name": "AncientYirgacheffeNeedle",
        }
    )
    for i in range(1100):
        store.append_history_event(
            {
                "event_id": f"bh_new_{i}",
                "outcome": "completed",
                "source": "local-skill",
                "recipe_name": f"Recent {i}",
            }
        )
    assert store.count_history_events() == 1101
    found = store.list_history_events(limit=5, query="YirgacheffeNeedle")
    assert len(found) == 1
    assert found[0]["event_id"] == "bh_old_match"
    store.close()


def test_history_event_from_workflow_terminal_separates_snapshot_and_recipe_sha():
    row = {
        "workflow_id": "wf_sha",
        "kind": "coffee",
        "source": "local-skill",
        "snapshot_sha256": "snap_digest_abc",
        "recipe_revision_id": "rev_1",
        "snapshot": {
            "name": "Path Recipe",
            "_source_sha256": "recipe_file_digest",
            "dose_g": 15,
        },
        "metadata": {"recipe_path": "/tmp/r.yaml"},
    }
    event = storage.history_event_from_workflow_terminal(
        row, state="ready", terminal_at="2026-01-01T00:00:00+00:00"
    )
    assert event["snapshot_sha256"] == "snap_digest_abc"
    assert event["recipe_sha256"] == "recipe_file_digest"
    assert event["snapshot_sha256"] != event["recipe_sha256"]

    # Revision-only recipes may lack recipe/source sha while snapshot sha remains.
    rev_only = {
        "workflow_id": "wf_rev",
        "kind": "coffee",
        "source": "local-skill",
        "snapshot_sha256": "snap_only",
        "recipe_revision_id": "rev_2",
        "snapshot": {"name": "Revision Only", "dose_g": 16},
        "metadata": {},
    }
    rev_event = storage.history_event_from_workflow_terminal(
        rev_only, state="ready", terminal_at="2026-01-01T00:00:00+00:00"
    )
    assert rev_event["snapshot_sha256"] == "snap_only"
    assert "recipe_sha256" not in rev_event


def test_terminal_reentry_identical_idempotent_conflict_raises(tmp_path):
    store = storage.StateStore(tmp_path)
    wf = store.create_workflow(
        kind="coffee",
        state="running",
        snapshot={"name": "T", "_source_sha256": "rec_sha"},
        source="local-skill",
    )
    first = store.commit_workflow_terminal(
        wf["workflow_id"],
        state="ready",
        event_type="terminal",
        event_payload={"result": "ready", "activity": "coffee"},
    )
    assert first.get("reentered") is not True
    hist_key = storage.workflow_terminal_history_dedupe_key(wf["workflow_id"])
    hist = store.get_history_event_by_dedupe_key(hist_key)
    assert hist is not None
    assert hist["snapshot_sha256"] == first["history_event"]["snapshot_sha256"]
    assert hist.get("recipe_sha256") == "rec_sha"
    events_before = store.list_workflow_events(wf["workflow_id"])

    again = store.commit_workflow_terminal(
        wf["workflow_id"],
        state="ready",
        event_type="terminal",
        event_payload={"result": "ready", "activity": "coffee"},
    )
    assert again.get("reentered") is True
    assert again["history_event"]["event_id"] == hist["event_id"]
    assert store.count_history_events() == 1
    assert len(store.list_workflow_events(wf["workflow_id"])) == len(events_before)
    loaded = store.get_workflow(wf["workflow_id"])
    assert loaded["state"] == "ready"
    assert loaded["terminal_at"] == first["terminal_at"]

    with pytest.raises(storage.StorageConflictError, match="already terminal"):
        store.commit_workflow_terminal(
            wf["workflow_id"],
            state="cancel_sent",
            event_type="terminal",
            event_payload={"result": "cancel_sent"},
        )
    assert store.get_workflow(wf["workflow_id"])["state"] == "ready"
    assert store.count_history_events() == 1
    store.close()


def test_concurrent_app_import_loser_reports_skipped_existing(tmp_path):
    bootstrap = storage.StateStore(tmp_path)
    bootstrap.ensure_schema()
    bootstrap.close()

    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        local = storage.StateStore(tmp_path)
        try:
            barrier.wait(timeout=5)
            out = local.import_app_history_records(
                [
                    {
                        "remote_table_id": 42,
                        "recipe_name": "Race Brew",
                        "serving_kind": "coffee",
                        "dose_g": 15,
                    }
                ],
                region="china",
            )
            results.append(out)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            local.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert len(results) == 2
    imported_total = sum(r["imported"] for r in results)
    skipped_total = sum(r["skipped_existing"] for r in results)
    assert imported_total == 1
    assert skipped_total == 1
    store = storage.StateStore(tmp_path)
    assert store.count_history_events() == 1
    store.close()


def test_concurrent_history_writers_independent_stores(tmp_path):
    bootstrap = storage.StateStore(tmp_path)
    bootstrap.ensure_schema()
    bootstrap.close()

    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(n: int) -> None:
        local = storage.StateStore(tmp_path)
        try:
            barrier.wait(timeout=5)
            for i in range(25):
                local.append_history_event(
                    {
                        "event_id": f"bh_{n}_{i}",
                        "outcome": "completed",
                        "source": "local-skill",
                        "recipe_name": f"R{n}-{i}",
                    }
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
    store = storage.StateStore(tmp_path)
    assert store.count_history_events() == 100
    store.close()


def test_facade_never_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    jsonl = tmp_path / "brew-history.jsonl"
    history.append_event(
        {
            "outcome": "completed",
            "source": "local-skill",
            "recipe_name": "No JSONL",
        },
        path=jsonl,
    )
    history.add_note(
        history.load_events(path=tmp_path)[0]["event_id"],
        "note",
        path=tmp_path,
    )
    assert not jsonl.exists()
    assert (tmp_path / "state.db").is_file()


def test_history_rejects_invalid_outcome(tmp_path):
    with pytest.raises(history.HistoryError, match="outcome"):
        history.append_event({"outcome": "not-a-real-outcome"}, path=tmp_path)

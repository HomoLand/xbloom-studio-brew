import json
from pathlib import Path

import xbloom
import xbloom_history as history


def test_history_status_list_and_note_round_trip(monkeypatch, tmp_path, capsys):
    path = tmp_path / "brew-history.jsonl"
    monkeypatch.setenv(history.HISTORY_PATH_ENV, str(path))
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

    assert xbloom.main(["history", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["total"] == 1
    assert status["by_outcome"]["completed"] == 1

    assert xbloom.main(["history", "list", "--limit", "5"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1
    assert listed["events"][0]["event_id"] == event["event_id"]

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
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 2
    assert rows[-1]["event_kind"] == "note"
    assert rows[-1]["note"] == "brighter citrus, a bit thin"


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
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["source"] == "app-cloud"

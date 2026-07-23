import asyncio
from pathlib import Path
import time

import pytest

import xbloom
import xbloom_history
import xbloom_ble.client as client_module
import xbloom_safety
from xbloom_ble.telemetry import StatusEvent


def _isolate_history(monkeypatch, tmp_path):
    """Point deprecated history path + state dir at tmp so no real state is used."""

    history_file = tmp_path / "brew-history.jsonl"
    monkeypatch.setenv(xbloom.HISTORY_PATH_ENV, str(history_file))
    monkeypatch.setenv(xbloom_history.HISTORY_PATH_ENV, str(history_file))
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    return history_file



class FakeTelemetryClient:
    def __init__(self, events):
        self.events = list(events)

    async def stream_telemetry(self, on_event, *, duration, stop_on_terminal):
        assert duration > 0
        assert stop_on_terminal is False
        for event in self.events:
            on_event(event)


def test_monitor_aggregates_weights_and_returns_terminal_summary(monkeypatch):
    emitted = []
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    client = FakeTelemetryClient(
        [
            StatusEvent(state=0x22, state_name="starting", raw=b"state"),
            StatusEvent(state=None, state_name="scale", raw=b"water", water_g=35.0),
            StatusEvent(state=None, state_name="scale", raw=b"coffee", coffee_g=12.12),
            StatusEvent(state=0x24, state_name="ready", raw=b"ready"),
        ]
    )

    result = asyncio.run(
        xbloom.monitor_client(client, 30, progress_interval=60)
    )

    assert result.terminal_confirmed is True
    assert result.completion_confirmed is True
    assert result.terminal_state == "ready"
    assert (result.water_g, result.coffee_g, result.events_seen) == (35.0, 12.12, 4)
    assert [item["state"] for item in emitted] == ["starting", "ready"]
    assert emitted[-1]["water_g"] == 35.0
    assert emitted[-1]["coffee_g"] == 12.12


def test_monitor_keeps_recipe_target_machine_meter_and_cup_delta_distinct(monkeypatch):
    emitted = []
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    client = FakeTelemetryClient(
        [
            StatusEvent(state=0x22, state_name="starting", raw=b"state"),
            StatusEvent(
                state=None,
                state_name="scale",
                raw=b"cup-baseline",
                cup_weight_g=28.0,
            ),
            StatusEvent(
                state=None,
                state_name="water_volume",
                raw=b"water",
                command_code=40523,
                dispensed_water_ml=150.0,
            ),
            StatusEvent(
                state=None,
                state_name="scale",
                raw=b"cup-final",
                cup_weight_g=164.5,
            ),
            StatusEvent(state=0x24, state_name="ready", raw=b"ready"),
        ]
    )

    result = asyncio.run(xbloom.monitor_client(client, 30, progress_interval=60))
    comparison = xbloom.volume_comparison(
        {"target_dispensed_water_ml": 152.0}, result
    )

    assert result.water_g == 150.0
    assert result.coffee_g == 164.5
    assert result.cup_delta_g == 136.5
    assert comparison == {
        "target_dispensed_water_ml": 152.0,
        "dispensed_water_ml": 150.0,
        "dispensed_vs_target_ml": -2.0,
        "cup_delta_g": 136.5,
        "cup_delta_to_dispensed_ratio": 0.91,
    }
    assert emitted[-1]["dispensed_water_ml"] == 150.0
    assert emitted[-1]["cup_weight_g"] == 164.5
    assert emitted[-1]["cup_delta_g"] == 136.5


def test_monitor_does_not_treat_initial_idle_as_completion(monkeypatch):
    monkeypatch.setattr(xbloom, "emit", lambda _data: None)
    client = FakeTelemetryClient(
        [
            StatusEvent(state=0x01, state_name="idle", raw=b"idle"),
            StatusEvent(state=None, state_name="scale", raw=b"weight", coffee_g=0.0),
        ]
    )

    result = asyncio.run(xbloom.monitor_client(client, 1, progress_interval=1))

    assert result.completion_confirmed is False
    assert result.saw_active is False
    assert result.last_state == "idle"


def test_idle_after_activity_is_terminal_but_not_success_confirmation(monkeypatch):
    monkeypatch.setattr(xbloom, "emit", lambda _data: None)
    client = FakeTelemetryClient(
        [
            StatusEvent(state=0x22, state_name="starting", raw=b"starting"),
            StatusEvent(state=0x01, state_name="idle", raw=b"idle"),
        ]
    )

    result = asyncio.run(xbloom.monitor_client(client, 1, progress_interval=1))

    assert result.terminal_confirmed is True
    assert result.terminal_state == "idle"
    assert result.completion_confirmed is False


def test_resolve_address_helpers_removed():
    """Passive scan is the only client-side discovery; resolve helpers are gone."""

    assert not hasattr(xbloom, "resolve_address")
    assert not hasattr(xbloom, "resolve_control_address")


@pytest.mark.parametrize(
    "workflow_phase",
    ["starting", "control_unconfirmed", "running"],
)
def test_monitor_observes_bridge_without_connecting(
    monkeypatch, tmp_path, workflow_phase
):
    """A9: monitor polls status/events only; never starts BLE or mutates workflow."""

    _isolate_history(monkeypatch, tmp_path)
    coffee_state = tmp_path / "armed-state.json"
    tea_state = tmp_path / "tea-loaded-state.json"
    ensure_calls = []
    connect_calls = []

    class FakeTyped:
        def ensure_daemon(self):
            ensure_calls.append("ensure")
            raise AssertionError("monitor must not ensure daemon")

        def connect(self, **kwargs):
            connect_calls.append(kwargs)
            raise AssertionError("monitor must not connect")

        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_observe",
                "phase": workflow_phase,
                "activity": "coffee",
                "connected": True,
                "telemetry": {"dispensed_water_peak_ml": 12.0},
                "liquid_progress": {"dispensed_water_ml": 12.0},
            }

        def events(self, *, since=0, workflow_id=None):
            assert workflow_id == "wf_observe"
            return {
                "events": [
                    {
                        "seq": 1,
                        "event_type": "terminal",
                        "payload": {"result": "ready", "state": "ready"},
                    }
                ],
                "next_since": 1,
                "gap_detected": False,
                "source": "durable",
            }

    emitted = []
    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FakeTyped())
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    args = xbloom.build_parser().parse_args(
        ["monitor", "--duration", "1", "--workflow-id", "wf_observe"]
    )

    assert asyncio.run(xbloom.async_monitor(args)) == 0
    assert ensure_calls == []
    assert connect_calls == []
    # Observation-only: never creates/clears coffee/tea JSON.
    assert not coffee_state.exists()
    assert not tea_state.exists()
    assert any(e.get("observation_only") for e in emitted)
    assert any(e.get("daemon_untouched") for e in emitted)
    assert emitted[0].get("status") == "listening"


def test_monitor_emits_periodic_telemetry_without_durable_events(monkeypatch, tmp_path):
    _isolate_history(monkeypatch, tmp_path)
    ticks = {"n": 0}

    class FakeTyped:
        def status(self, *, require_hello=False):
            ticks["n"] += 1
            if ticks["n"] >= 3:
                return {
                    "active_workflow_id": "wf_obs",
                    "phase": "idle",
                    "activity": None,
                    "connected": False,
                    "last_operation": {
                        "workflow_id": "wf_obs",
                        "result": "ready",
                    },
                    "telemetry": {"dispensed_water_peak_ml": 40.0},
                    "liquid_progress": {"dispensed_water_ml": 40.0},
                }
            return {
                "active_workflow_id": "wf_obs",
                "phase": "running",
                "activity": "coffee",
                "connected": True,
                "telemetry": {"dispensed_water_peak_ml": float(ticks["n"] * 10)},
                "liquid_progress": {"dispensed_water_ml": float(ticks["n"] * 10)},
            }

        def events(self, *, since=0, workflow_id=None):
            return {
                "events": [],
                "next_since": since,
                "gap_detected": False,
                "source": "durable",
            }

    emitted = []
    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FakeTyped())
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    args = xbloom.build_parser().parse_args(
        ["monitor", "--duration", "5", "--progress-interval", "0.1", "--workflow-id", "wf_obs"]
    )
    assert asyncio.run(xbloom.async_monitor(args)) == 0
    progress = [e for e in emitted if e.get("command") == "monitor-progress"]
    assert progress, "monitor must emit rate-limited progress from status telemetry"
    assert any(
        (p.get("telemetry") or {}).get("dispensed_water_peak_ml") is not None
        for p in progress
    )


def test_monitor_rejects_stale_workflow_id(monkeypatch, tmp_path):
    class FakeTyped:
        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_active",
                "phase": "running",
                "activity": "coffee",
            }

        def events(self, *, since=0, workflow_id=None):
            raise AssertionError("events must not be polled for stale workflow")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FakeTyped())
    args = xbloom.build_parser().parse_args(
        ["monitor", "--duration", "1", "--workflow-id", "wf_stale"]
    )
    with pytest.raises(RuntimeError, match="not the active workflow"):
        asyncio.run(xbloom.async_monitor(args))


def test_monitor_requires_running_daemon(monkeypatch, tmp_path):
    class BoomTyped:
        def status(self, *, require_hello=False):
            raise RuntimeError("no valid bridge record")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: BoomTyped())
    args = xbloom.build_parser().parse_args(["monitor", "--duration", "10"])

    with pytest.raises(RuntimeError, match="running bridge daemon"):
        asyncio.run(xbloom.async_monitor(args))


def test_monitor_requires_workflow_identity(monkeypatch, tmp_path):
    class IdleTyped:
        def status(self, *, require_hello=False):
            return {"active_workflow_id": None, "phase": "disconnected", "activity": None}

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: IdleTyped())
    args = xbloom.build_parser().parse_args(["monitor", "--duration", "1"])
    with pytest.raises(RuntimeError, match="workflow-id|active durable"):
        asyncio.run(xbloom.async_monitor(args))


def test_monitor_status_failures_independent_of_events(monkeypatch, tmp_path):
    """Successful events must not reset a permanently failing status counter."""

    status_calls = {"n": 0}

    class FakeTyped:
        def status(self, *, require_hello=False):
            status_calls["n"] += 1
            # Initial identity probe succeeds once.
            if status_calls["n"] == 1:
                return {
                    "active_workflow_id": "wf_obs",
                    "phase": "running",
                    "activity": "coffee",
                    "connected": True,
                }
            raise RuntimeError("status permanently unavailable")

        def events(self, *, since=0, workflow_id=None):
            assert workflow_id == "wf_obs"
            return {
                "events": [],
                "next_since": since,
                "gap_detected": False,
                "source": "durable",
            }

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FakeTyped())
    args = xbloom.build_parser().parse_args(
        [
            "monitor",
            "--duration",
            "30",
            "--progress-interval",
            "0.1",
            "--workflow-id",
            "wf_obs",
        ]
    )
    with pytest.raises(RuntimeError, match="status failed repeatedly"):
        asyncio.run(xbloom.async_monitor(args))
    # Initial + 3 consecutive in-loop failures (threshold 3).
    assert status_calls["n"] == 1 + 3


def test_monitor_idle_foreign_last_op_no_global_fields(monkeypatch, tmp_path):
    """Idle daemon with last_operation for a different workflow: durable events
    may finish observation, but never attach foreign phase/telemetry/connected.
    """

    class FakeTyped:
        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": None,
                "phase": "idle",
                "activity": None,
                "connected": True,
                "machine_state": "idle",
                "telemetry": {"dispensed_water_peak_ml": 99.0},
                "liquid_progress": {"dispensed_water_ml": 99.0},
                "last_operation": {
                    "workflow_id": "wf_other",
                    "result": "complete",
                },
            }

        def events(self, *, since=0, workflow_id=None):
            assert workflow_id == "wf_hist"
            return {
                "events": [
                    {
                        "seq": 1,
                        "event_type": "terminal",
                        "payload": {"result": "ready", "state": "ready"},
                    }
                ],
                "next_since": 1,
                "gap_detected": False,
                "source": "durable",
            }

    emitted = []
    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FakeTyped())
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    args = xbloom.build_parser().parse_args(
        [
            "monitor",
            "--duration",
            "5",
            "--progress-interval",
            "0.1",
            "--workflow-id",
            "wf_hist",
        ]
    )
    assert asyncio.run(xbloom.async_monitor(args)) == 0
    progress = [e for e in emitted if e.get("command") == "monitor-progress"]
    finals = [
        e
        for e in emitted
        if e.get("command") == "monitor" and e.get("status") != "listening"
    ]
    assert finals and finals[-1].get("status") == "ready"
    for row in progress + finals:
        assert "phase" not in row
        assert "activity" not in row
        assert "connected" not in row
        assert "telemetry" not in row
        assert "liquid_progress" not in row
        assert "machine_state" not in row


def test_load_rejects_empty_workflow_id(monkeypatch, tmp_path):
    """Exact-workflow contract: missing workflow_id refuses success; no JSON write."""

    _isolate_history(monkeypatch, tmp_path)
    json_paths = [
        tmp_path / "armed-state.json",
        tmp_path / "tea-loaded-state.json",
        tmp_path / "grinder-rest-state.json",
    ]

    class FakeTyped:
        def coffee_load(self, **kwargs):
            return {"status": "armed", "workflow_id": ""}

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(
        xbloom,
        "load_recipe",
        lambda _path: (
            Path("recipe.yaml"),
            object(),
            {
                "recipe_sha256": "h",
                "target_dispensed_water_ml": 240,
                "kind": "hot",
                "machine_program": "omni",
                "manual_preload_ice_g": 0,
            },
        ),
    )
    args = xbloom.build_parser().parse_args(["load", "recipe.yaml"])
    with pytest.raises(RuntimeError, match="no workflow_id"):
        asyncio.run(xbloom.async_load(args))
    for path in json_paths:
        assert not path.exists()


def test_tea_load_rejects_missing_workflow_id(monkeypatch, tmp_path):
    _isolate_history(monkeypatch, tmp_path)
    tea_json = tmp_path / "tea-loaded-state.json"

    class FakeTyped:
        def tea_load(self, **kwargs):
            return {"status": "tea_loaded"}  # workflow_id absent

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(
        xbloom,
        "load_tea_recipe",
        lambda _path: (
            Path("tea.yaml"),
            object(),
            {
                "recipe_sha256": "th",
                "programmed_water_ml": 80,
            },
        ),
    )
    args = xbloom.build_parser().parse_args(["tea-load", "tea.yaml"])
    with pytest.raises(RuntimeError, match="no workflow_id"):
        asyncio.run(xbloom.async_tea_load(args))
    assert not tea_json.exists()


def test_water_timeout_is_observation_bound_no_side_effects(monkeypatch, tmp_path):
    """water --timeout observes the exact workflow; never cancel/release."""

    _isolate_history(monkeypatch, tmp_path)
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    side_effects = []
    emitted = []

    class FakeTyped:
        def water_start(self, **kwargs):
            return {
                "status": "running",
                "workflow_id": "wf_water_1",
                "safety_timeout_s": 90,
            }

        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_water_1",
                "phase": "idle",
                "activity": None,
                "connected": False,
                "last_operation": {
                    "workflow_id": "wf_water_1",
                    "result": "complete",
                },
            }

        def events(self, *, since=0, workflow_id=None):
            assert workflow_id == "wf_water_1"
            return {
                "events": [
                    {
                        "seq": 1,
                        "event_type": "terminal",
                        "payload": {"result": "complete", "state": "complete"},
                    }
                ],
                "next_since": 1,
                "gap_detected": False,
                "source": "durable",
            }

        def cancel(self, **kwargs):
            side_effects.append(("cancel", kwargs))
            return {"status": "cancel_sent"}

        def ensure_daemon(self):
            side_effects.append("ensure")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    args = xbloom.build_parser().parse_args(
        [
            "water",
            "--volume",
            "120",
            "--temp",
            "85",
            "--flow",
            "3.5",
            "--confirm-ready",
            xbloom.WATER_READY_SENTINEL,
            "--timeout",
            "15",
        ]
    )
    assert asyncio.run(xbloom.async_water(args)) == 0
    assert side_effects == []
    start_rows = [e for e in emitted if e.get("command") == "water"]
    assert start_rows and start_rows[0]["workflow_id"] == "wf_water_1"
    assert start_rows[0]["observation_bound_s"] == 15.0
    finals = [
        e
        for e in emitted
        if e.get("command") == "monitor" and e.get("status") != "listening"
    ]
    assert finals and finals[-1].get("workflow_id") == "wf_water_1"
    assert finals[-1].get("daemon_untouched") is True


def test_water_rejects_empty_workflow_id_before_monitor(monkeypatch, tmp_path):
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    monitor_calls = []

    class FakeTyped:
        def water_start(self, **kwargs):
            return {"status": "running", "workflow_id": "  "}

    async def boom_monitor(args):
        monitor_calls.append(args)
        raise AssertionError("must not monitor without workflow_id")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(xbloom, "async_monitor", boom_monitor)
    args = xbloom.build_parser().parse_args(
        [
            "water",
            "--volume",
            "120",
            "--temp",
            "85",
            "--confirm-ready",
            xbloom.WATER_READY_SENTINEL,
        ]
    )
    with pytest.raises(RuntimeError, match="no workflow_id"):
        asyncio.run(xbloom.async_water(args))
    assert monitor_calls == []


def test_start_uses_typed_client_workflow_id(monkeypatch, tmp_path):
    """A9: start passes durable workflow_id and never reads coffee JSON or recipe path."""

    _isolate_history(monkeypatch, tmp_path)
    coffee_state = tmp_path / "armed-state.json"
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    calls = []
    load_recipe_calls = []

    class FakeTyped:
        def coffee_start(self, **kwargs):
            calls.append(("coffee_start", kwargs))
            assert kwargs["workflow_id"] == "wf_start_1"
            return {
                "status": "running",
                "workflow_id": "wf_start_1",
                "machine_program": "coffee-pour-over",
                "manual_preload_ice_g": 0,
            }

        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_start_1",
                "phase": "idle",
                "activity": None,
                "connected": False,
                "last_operation": {
                    "workflow_id": "wf_start_1",
                    "result": "ready",
                },
            }

        def events(self, *, since=0, workflow_id=None):
            return {"events": [], "next_since": since}

    def boom_load_recipe(path):
        load_recipe_calls.append(path)
        raise AssertionError("start must not open the legacy positional recipe")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FakeTyped())
    monkeypatch.setattr(xbloom, "load_recipe", boom_load_recipe)
    monkeypatch.setattr(xbloom, "emit", lambda _data: None)
    args = xbloom.build_parser().parse_args(
        [
            "start",
            "recipe.yaml",
            "--workflow-id",
            "wf_start_1",
            "--confirm-ready",
            xbloom.READY_SENTINEL,
            "--duration",
            "1",
        ]
    )

    assert asyncio.run(xbloom.async_start(args)) == 0
    assert calls and calls[0][0] == "coffee_start"
    assert calls[0][1]["workflow_id"] == "wf_start_1"
    assert load_recipe_calls == []
    assert not coffee_state.exists()


def test_start_failure_propagates_without_coffee_json_write(monkeypatch, tmp_path):
    """Start failure is bridge-owned; CLI never writes coffee JSON or local retry gate."""

    _isolate_history(monkeypatch, tmp_path)
    coffee_state = tmp_path / "armed-state.json"
    tea_state = tmp_path / "tea-loaded-state.json"
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    calls = []

    class FailingTyped:
        def coffee_start(self, **kwargs):
            calls.append(("coffee_start", kwargs))
            assert kwargs["workflow_id"] == "wf_fail"
            raise RuntimeError("start acknowledgement lost; do not retry start")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _args: FailingTyped())
    monkeypatch.setattr(
        xbloom,
        "load_recipe",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("start must not open positional recipe")
        ),
    )
    args = xbloom.build_parser().parse_args(
        [
            "start",
            "missing-or-ignored.yaml",
            "--workflow-id",
            "wf_fail",
            "--confirm-ready",
            xbloom.READY_SENTINEL,
        ]
    )

    with pytest.raises(RuntimeError, match="acknowledgement lost"):
        asyncio.run(xbloom.async_start(args))
    assert len(calls) == 1
    assert calls[0][0] == "coffee_start"
    assert calls[0][1]["workflow_id"] == "wf_fail"
    # No local coffee/tea JSON write; retry protection belongs to the bridge.
    assert not coffee_state.exists()
    assert not tea_state.exists()


def test_start_ignores_nonexistent_positional_recipe(monkeypatch, tmp_path):
    """Positional recipe is CLI compatibility only; nonexistent path is never read."""

    _isolate_history(monkeypatch, tmp_path)
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    missing = tmp_path / "does-not-exist.yaml"
    assert not missing.exists()
    calls = []
    load_calls = []

    class FakeTyped:
        def coffee_start(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["workflow_id"] == "wf_exact"
            return {"status": "running", "workflow_id": "wf_exact"}

        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_exact",
                "phase": "idle",
                "activity": None,
                "connected": False,
                "last_operation": {
                    "workflow_id": "wf_exact",
                    "result": "ready",
                },
            }

        def events(self, *, since=0, workflow_id=None):
            return {"events": [], "next_since": since}

    def boom_load(path):
        load_calls.append(path)
        raise AssertionError(f"must not open positional recipe {path!r}")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(xbloom, "load_recipe", boom_load)
    monkeypatch.setattr(xbloom, "emit", lambda _d: None)
    args = xbloom.build_parser().parse_args(
        [
            "start",
            str(missing),
            "--workflow-id",
            "wf_exact",
            "--confirm-ready",
            xbloom.READY_SENTINEL,
            "--duration",
            "1",
        ]
    )
    assert asyncio.run(xbloom.async_start(args)) == 0
    assert load_calls == []
    assert calls and calls[0]["workflow_id"] == "wf_exact"
    assert not (tmp_path / "armed-state.json").exists()


def test_start_ignores_mutated_positional_recipe(monkeypatch, tmp_path):
    """Mutated positional recipe content is never hashed or validated on start."""

    _isolate_history(monkeypatch, tmp_path)
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("name: Original\n", encoding="utf-8")
    calls = []
    hash_calls = []

    class FakeTyped:
        def coffee_start(self, **kwargs):
            calls.append(kwargs)
            return {"status": "running", "workflow_id": "wf_mut"}

        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_mut",
                "phase": "idle",
                "activity": None,
                "connected": False,
                "last_operation": {
                    "workflow_id": "wf_mut",
                    "result": "ready",
                },
            }

        def events(self, *, since=0, workflow_id=None):
            return {"events": [], "next_since": since}

    def boom_hash(path):
        hash_calls.append(path)
        raise AssertionError("must not hash positional recipe")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(
        xbloom,
        "load_recipe",
        lambda _p: (_ for _ in ()).throw(AssertionError("must not load recipe")),
    )
    monkeypatch.setattr(xbloom_safety, "recipe_sha256", boom_hash)
    monkeypatch.setattr(xbloom, "emit", lambda _d: None)
    recipe.write_text("name: Mutated after load\ndose_g: 99\n", encoding="utf-8")
    args = xbloom.build_parser().parse_args(
        [
            "start",
            str(recipe),
            "--workflow-id",
            "wf_mut",
            "--confirm-ready",
            xbloom.READY_SENTINEL,
            "--duration",
            "1",
        ]
    )
    assert asyncio.run(xbloom.async_start(args)) == 0
    assert hash_calls == []
    assert calls and calls[0]["workflow_id"] == "wf_mut"


def test_tea_start_ignores_nonexistent_positional_recipe(monkeypatch, tmp_path):
    """Symmetric tea-start: positional recipe ignored; exact workflow_id sent."""

    _isolate_history(monkeypatch, tmp_path)
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    missing = tmp_path / "tea-missing.yaml"
    assert not missing.exists()
    calls = []
    load_calls = []

    class FakeTyped:
        def tea_start(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["workflow_id"] == "wf_tea_exact"
            return {"status": "running", "workflow_id": "wf_tea_exact"}

        def status(self, *, require_hello=False):
            return {
                "active_workflow_id": "wf_tea_exact",
                "phase": "idle",
                "activity": None,
                "connected": False,
                "last_operation": {
                    "workflow_id": "wf_tea_exact",
                    "result": "ready",
                },
            }

        def events(self, *, since=0, workflow_id=None):
            return {"events": [], "next_since": since}

    def boom_load(path):
        load_calls.append(path)
        raise AssertionError(f"must not open positional tea recipe {path!r}")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(xbloom, "load_tea_recipe", boom_load)
    monkeypatch.setattr(xbloom, "emit", lambda _d: None)
    args = xbloom.build_parser().parse_args(
        [
            "tea-start",
            str(missing),
            "--workflow-id",
            "wf_tea_exact",
            "--confirm-ready",
            xbloom.TEA_READY_SENTINEL,
            "--duration",
            "1",
        ]
    )
    assert asyncio.run(xbloom.async_tea_start(args)) == 0
    assert load_calls == []
    assert calls and calls[0]["workflow_id"] == "wf_tea_exact"
    assert not (tmp_path / "tea-loaded-state.json").exists()

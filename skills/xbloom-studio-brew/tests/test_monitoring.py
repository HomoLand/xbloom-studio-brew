import asyncio
from pathlib import Path
import time

import pytest

import xbloom
import xbloom_ble.client as client_module
import xbloom_safety
from xbloom_ble.telemetry import StatusEvent


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


def test_monitor_and_cancel_reuse_loaded_workflow_address(monkeypatch, tmp_path):
    coffee_state = tmp_path / "coffee.json"
    tea_state = tmp_path / "tea.json"
    xbloom.state_write(
        {"address": "recorded-device", "machine": "xBloom", "status": "armed"},
        coffee_state,
    )
    monkeypatch.setattr(xbloom, "STATE_FILE", coffee_state)
    monkeypatch.setattr(xbloom, "TEA_STATE_FILE", tea_state)
    monkeypatch.delenv("XBLOOM_ADDRESS", raising=False)

    async def unexpected_scan(_explicit, _timeout):
        raise AssertionError("recovery must not scan while a workflow record exists")

    monkeypatch.setattr(xbloom, "resolve_address", unexpected_scan)
    assert asyncio.run(xbloom.resolve_control_address(None, 1)) == (
        "recorded-device",
        "xBloom",
    )
    assert asyncio.run(xbloom.resolve_control_address("RECORDED-DEVICE", 1))[0] == (
        "recorded-device"
    )
    with pytest.raises(RuntimeError, match="differs from the loaded workflow"):
        asyncio.run(xbloom.resolve_control_address("other-device", 1))


def test_monitor_reattach_uses_active_hint_and_clears_confirmed_state(
    monkeypatch, tmp_path
):
    coffee_state = tmp_path / "coffee.json"
    tea_state = tmp_path / "tea.json"
    monkeypatch.setattr(xbloom, "STATE_FILE", coffee_state)
    monkeypatch.setattr(xbloom, "TEA_STATE_FILE", tea_state)
    xbloom.state_write(
        {
            "address": "recorded-device",
            "machine": "xBloom",
            "status": "completion_unconfirmed",
        }
    )

    class FakeMonitorClient:
        def __init__(self, address):
            assert address == "recorded-device"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def fake_monitor(_client, _duration, **kwargs):
        assert kwargs["active_already"] is True
        return xbloom.MonitorResult(
            terminal_confirmed=True,
            terminal_state="ready",
            last_state="ready",
            saw_active=True,
            water_g=150.0,
            coffee_g=116.6,
            scale_g=None,
            events_seen=1,
            elapsed_s=0.1,
        )

    emitted = []
    monkeypatch.setattr(client_module, "XBloomClient", FakeMonitorClient)
    monkeypatch.setattr(xbloom, "monitor_client", fake_monitor)
    monkeypatch.setattr(xbloom, "emit", emitted.append)
    args = xbloom.build_parser().parse_args(["monitor", "--duration", "10"])

    assert asyncio.run(xbloom.async_monitor(args)) == 0
    assert not coffee_state.exists()
    assert emitted[-1]["terminal_confirmed"] is True
    assert emitted[-1]["completion_confirmed"] is True
    assert emitted[-1]["state_records_cleared"] == 1


def test_monitor_refuses_ambiguous_dual_workflow_records(monkeypatch, tmp_path):
    coffee_state = tmp_path / "coffee.json"
    tea_state = tmp_path / "tea.json"
    monkeypatch.setattr(xbloom, "STATE_FILE", coffee_state)
    monkeypatch.setattr(xbloom, "TEA_STATE_FILE", tea_state)
    for path in (coffee_state, tea_state):
        xbloom.state_write(
            {"address": "recorded-device", "status": "completion_unconfirmed"},
            path,
        )
    args = xbloom.build_parser().parse_args(["monitor", "--duration", "10"])

    with pytest.raises(RuntimeError, match="multiple loaded workflow records"):
        asyncio.run(xbloom.async_monitor(args))


@pytest.mark.parametrize(
    ("terminal_confirmed", "terminal_state", "expected_rc", "state_exists"),
    [
        (True, "ready", 0, False),
        (False, None, xbloom.UNCONFIRMED_COMPLETION_EXIT, True),
    ],
)
def test_start_clears_state_only_after_terminal_confirmation(
    monkeypatch,
    tmp_path,
    terminal_confirmed,
    terminal_state,
    expected_rc,
    state_exists,
):
    state_path = tmp_path / "armed.json"
    monkeypatch.setattr(xbloom, "STATE_FILE", state_path)
    monkeypatch.setenv(xbloom.REMOTE_START_ENV, xbloom.REMOTE_START_SENTINEL)
    xbloom.state_write(
        {
            "address": "recorded-device",
            "machine": "xBloom",
            "recipe_sha256": "same-hash",
            "loaded_at": time.time(),
            "status": "armed",
        }
    )

    class FakeStartClient:
        def __init__(self, address):
            assert address == "recorded-device"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def start(self):
            return StatusEvent(state=0x22, state_name="starting", raw=b"verified")

    async def fake_monitor(_client, _duration, **_kwargs):
        return xbloom.MonitorResult(
            terminal_confirmed=terminal_confirmed,
            terminal_state=terminal_state,
            last_state=terminal_state,
            saw_active=True,
            water_g=150.0,
            coffee_g=116.6,
            scale_g=None,
            events_seen=100,
            elapsed_s=10.0,
        )

    monkeypatch.setattr(client_module, "XBloomClient", FakeStartClient)
    monkeypatch.setattr(xbloom, "monitor_client", fake_monitor)
    monkeypatch.setattr(
        xbloom,
        "load_recipe",
        lambda _path: (Path("recipe.yaml"), object(), {"recipe_sha256": "same-hash"}),
    )
    monkeypatch.setattr(xbloom_safety, "recipe_sha256", lambda _path: "same-hash")
    monkeypatch.setattr(xbloom, "emit", lambda _data: None)
    args = xbloom.build_parser().parse_args(
        [
            "start",
            "recipe.yaml",
            "--confirm-ready",
            xbloom.READY_SENTINEL,
        ]
    )

    assert asyncio.run(xbloom.async_start(args)) == expected_rc
    assert state_path.exists() is state_exists
    if state_exists:
        saved = xbloom.state_read()
        assert saved["status"] == "completion_unconfirmed"
        assert saved["last_state"] == "starting"

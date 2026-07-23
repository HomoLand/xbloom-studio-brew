"""Phase A9 typed bridge client + Skill CLI convergence contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from xbloom_ble.bridge import BridgeCore, BridgeError, BridgeServer, bridge_call
from xbloom_ble.bridge_client import TypedBridgeClient, new_request_id
from xbloom_ble.telemetry import StatusEvent
import xbloom


def _event(
    *,
    command: int | None = None,
    state: int | None = None,
    name: str = "ack",
    machine_info: dict | None = None,
) -> StatusEvent:
    return StatusEvent(
        state=state,
        state_name=name,
        raw=b"test",
        command_code=command,
        machine_info=machine_info,
    )


class FakeBridgeClient:
    def __init__(self, address: str):
        self.address = address
        self.is_connected = False
        self.listeners = set()
        self.disconnect_listeners = set()
        self.calls: list = []
        self.machine_info = {
            "firmware": "V12.0D.500",
            "water_source": "tank",
            "weight_unit": "g",
            "temperature_unit": "C",
            "display": "medium",
            "pouring_radius_init": 680,
            "vibration_init": 1000,
            "serial_number": "SECRET",
        }

    def add_event_listener(self, listener):
        self.listeners.add(listener)

    def remove_event_listener(self, listener):
        self.listeners.discard(listener)

    def add_disconnect_listener(self, listener):
        self.disconnect_listeners.add(listener)

    def remove_disconnect_listener(self, listener):
        self.disconnect_listeners.discard(listener)

    def mark_disconnect_expected(self):
        pass

    def emit(self, event: StatusEvent):
        for listener in tuple(self.listeners):
            listener(event)

    async def connect(self):
        self.is_connected = True
        self.calls.append("connect")

    async def disconnect(self):
        self.is_connected = False
        self.calls.append("disconnect")

    def drop_link(self):
        self.is_connected = False
        self.calls.append("drop_link")
        for listener in tuple(self.disconnect_listeners):
            listener(False)

    async def open_session(self):
        self.calls.append("open_session")

    async def close_session(self):
        self.calls.append("close_session")

    async def request_status(self):
        self.calls.append("request_status")
        self.emit(
            _event(
                command=40521,
                state=0x01,
                name="idle",
                machine_info=dict(self.machine_info),
            )
        )

    async def read_machine_info(self):
        self.calls.append("read_machine_info")
        return dict(self.machine_info)

    async def load_recipe(self, recipe):
        self.calls.append(("load_recipe", recipe.name))
        event = _event(state=0x1F, name="armed")
        self.emit(event)
        return event

    async def start(self):
        self.calls.append("coffee_start")
        event = _event(state=0x22, name="starting")
        self.emit(event)
        return event

    async def cancel_brew(self):
        self.calls.append("coffee_cancel")

    async def load_tea_recipe(self, recipe):
        self.calls.append(("tea_load", recipe.name))
        event = _event(command=4513, name="tea_recipe_code")
        self.emit(event)
        return event

    async def start_tea(self):
        self.calls.append("tea_start")
        event = _event(command=4512, name="tea_recipe_make")
        self.emit(event)
        return event

    async def set_machine_settings(self, **requested):
        self.calls.append(("set_machine_settings", dict(requested)))
        self.machine_info.update(requested)
        return dict(self.machine_info)

    async def read_advanced_settings(self):
        return {"pour_radius": 680, "vibration_amplitude": 1000}

    async def write_advanced_settings(self, **requested):
        return await self.read_advanced_settings()

    async def start_grinder_session(self, size, rpm):
        self.calls.append(("grinder_start", size, rpm))
        return _event(command=3500)

    async def stop_grinder_session(self):
        self.calls.append("grinder_stop")
        return _event(command=3505)

    async def pause_grinder(self):
        return _event(command=8018)

    async def resume_grinder(self):
        return _event(command=8020)

    async def start_water_session(self, volume, temp, **kwargs):
        self.calls.append(("water_start", volume, temp, kwargs))

    async def stop_water_session(self):
        self.calls.append("water_stop")
        event = _event(command=4507, name="brewer_stop_echo")
        self.emit(event)
        return event

    async def quit_water_session(self):
        self.calls.append("water_quit")

    async def pause_water(self):
        return _event(command=8019)

    async def resume_water(self):
        return _event(command=8021)

    async def set_water_temperature(self, temp):
        return _event(command=8108)

    async def set_water_pattern(self, pattern):
        return _event(command=8107)

    async def stream_scale(self, on_event, *, duration, tare, on_ready):
        self.calls.append(("scale_start", duration, tare))
        await on_ready()
        event = _event(name="scale")
        event.scale_g = 1.0
        await on_event(event)
        await asyncio.sleep(0)

    async def tare_scale(self):
        self.calls.append("scale_tare")

    async def save_slots(self, recipes, *, scale=True):
        self.calls.append(("save_slots", [r.name for r in recipes], scale))


def _recipe(path: Path) -> Path:
    path.write_text(
        """name: A9 test
dose_g: 16
grind: 55
pours:
  - {ml: 40, temp_c: 92, pattern: spiral, pause_s: 30, rpm: 100, flow_ml_s: 3.0}
  - {ml: 100, temp_c: 92, pattern: spiral, pause_s: 5, rpm: 100, flow_ml_s: 3.0}
  - {ml: 100, temp_c: 92, pattern: spiral, pause_s: 5, rpm: 100, flow_ml_s: 3.0}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("XBLOOM_ENABLE_REMOTE_START", "I_ACCEPT_HOT_WATER_RISK")
    monkeypatch.setenv("XBLOOM_ENABLE_REMOTE_GRINDER", "I_ACCEPT_GRINDER_RISK")
    monkeypatch.setenv("XBLOOM_ENABLE_SETTINGS_WRITE", "I_ACCEPT_SETTINGS_WRITE_RISK")
    return tmp_path


def test_new_request_id_is_unique():
    a = new_request_id()
    b = new_request_id()
    assert a != b
    assert a.startswith("req_")


def test_typed_client_injects_request_id_and_requires_workflow(monkeypatch, bridge_env):
    calls: list[tuple[str, dict]] = []

    def fake_call(method, params=None, **kwargs):
        body = dict(params or {})
        calls.append((method, body))
        if method == "coffee.start":
            assert "request_id" in body
            assert body["workflow_id"] == "wf_1"
            return {"status": "running", "workflow_id": "wf_1", "request_id": body["request_id"]}
        raise BridgeError(f"unexpected {method}")

    monkeypatch.setattr("xbloom_ble.bridge_client.bridge_call", fake_call)
    monkeypatch.setattr(
        "xbloom_ble.bridge_client.ensure_bridge_daemon",
        lambda **k: {"running": True, "client_ready": True},
    )
    client = TypedBridgeClient(auto_ensure=True)
    client.coffee_start(workflow_id="wf_1", confirmation="cup-filter-water-beans")
    assert calls[0][0] == "coffee.start"
    assert calls[0][1]["request_id"]
    # Caller-supplied request_id is preserved.
    rid = "req_explicit_keep"
    client.coffee_start(
        workflow_id="wf_1",
        confirmation="cup-filter-water-beans",
        request_id=rid,
    )
    assert calls[1][1]["request_id"] == rid
    with pytest.raises(BridgeError, match="workflow_id"):
        # Bypass typed method to hit _call validation would require coffee_start
        # without workflow - typed API requires the arg. Exercise internal:
        client._call("coffee.start", {"confirmation": "cup-filter-water-beans"})


def test_typed_recipe_loads_support_revision_only_without_fake_paths(
    monkeypatch, bridge_env
):
    calls: list[tuple[str, dict]] = []
    ensures: list[int] = []

    def fake_call(method, params=None, **_kwargs):
        body = dict(params or {})
        calls.append((method, body))
        return {
            "status": "loaded",
            "workflow_id": f"wf_{len(calls)}",
            "request_id": body.get("request_id"),
        }

    monkeypatch.setattr("xbloom_ble.bridge_client.bridge_call", fake_call)
    monkeypatch.setattr(
        "xbloom_ble.bridge_client.ensure_bridge_daemon",
        lambda **_kwargs: ensures.append(1)
        or {"running": True, "client_ready": True},
    )
    client = TypedBridgeClient(state_root=bridge_env, auto_ensure=True)

    client.coffee_load(recipe_revision_id="rev_coffee")
    client.tea_load(recipe_revision_id="rev_tea", request_id="req_tea")

    assert calls[0][0] == "coffee.load"
    assert calls[0][1]["recipe_revision_id"] == "rev_coffee"
    assert calls[0][1]["request_id"]
    assert "recipe" not in calls[0][1]
    assert calls[1] == (
        "tea.load",
        {
            "request_id": "req_tea",
            "scan_timeout": 8.0,
            "recipe_revision_id": "rev_tea",
        },
    )
    assert ensures == [1, 1]

    with pytest.raises(BridgeError, match="recipe path or recipe_revision_id"):
        client.coffee_load()
    with pytest.raises(BridgeError, match="recipe path or recipe_revision_id"):
        client.tea_load(recipe="  ", recipe_revision_id="")
    assert len(calls) == 2
    assert ensures == [1, 1]


def test_status_events_do_not_ensure_daemon(monkeypatch, bridge_env):
    ensured = {"n": 0}

    def boom_ensure(**k):
        ensured["n"] += 1
        raise AssertionError("status must not ensure daemon")

    monkeypatch.setattr("xbloom_ble.bridge_client.ensure_bridge_daemon", boom_ensure)

    def fake_call(method, params=None, **kwargs):
        return {"ok": True, "method": method, "running": True}

    monkeypatch.setattr("xbloom_ble.bridge_client.bridge_call", fake_call)
    client = TypedBridgeClient(auto_ensure=True)
    client.status()
    client.events(since=0)
    assert ensured["n"] == 0


def test_ensure_daemon_rejects_client_ready_false_before_rpc(monkeypatch, bridge_env):
    calls = []

    def boom_call(*a, **k):
        calls.append((a, k))
        raise AssertionError("bridge_call must not run when client_ready=False")

    monkeypatch.setattr("xbloom_ble.bridge_client.bridge_call", boom_call)
    monkeypatch.setattr(
        "xbloom_ble.bridge_client.ensure_bridge_daemon",
        lambda **k: {
            "running": True,
            "client_ready": False,
            "reason": "legacy_or_incompatible_daemon_not_idle",
            "message": "running daemon is protocol-incompatible but busy",
            "upgrade_pending": True,
        },
    )
    client = TypedBridgeClient(auto_ensure=True)
    with pytest.raises(Exception) as ei:
        client.ensure_daemon()
    assert calls == []
    # Prefer BridgeCompatibilityError for protocol/upgrade paths.
    from xbloom_ble.bridge import BridgeCompatibilityError

    assert isinstance(ei.value, BridgeCompatibilityError)
    assert getattr(ei.value, "category", None) == "protocol_incompatible"
    with pytest.raises(Exception):
        client.coffee_load(recipe="x.yaml")
    assert calls == []


def test_disconnect_never_ensures_missing_daemon(monkeypatch, bridge_env):
    ensures = []

    monkeypatch.setattr(
        "xbloom_ble.bridge_client.ensure_bridge_daemon",
        lambda **k: ensures.append(k) or {"running": True, "client_ready": True},
    )
    monkeypatch.setattr(
        "xbloom_ble.bridge_client.bridge_is_running",
        lambda **k: False,
    )
    client = TypedBridgeClient(state_root=bridge_env, auto_ensure=True)
    with pytest.raises(BridgeError, match="no running bridge daemon"):
        client.disconnect()
    assert ensures == []


def test_long_lived_client_rechecks_daemon_each_hardware_rpc(monkeypatch, bridge_env):
    ensures = []
    calls = []

    def ensure(**k):
        ensures.append(1)
        if len(ensures) == 1:
            return {"running": True, "client_ready": True}
        return {
            "running": True,
            "client_ready": False,
            "reason": "protocol_incompatible",
            "message": "daemon became incompatible",
        }

    def fake_call(method, params=None, **kwargs):
        calls.append(method)
        return {"status": "ok", "method": method, "request_id": (params or {}).get("request_id")}

    monkeypatch.setattr("xbloom_ble.bridge_client.ensure_bridge_daemon", ensure)
    monkeypatch.setattr("xbloom_ble.bridge_client.bridge_call", fake_call)
    client = TypedBridgeClient(auto_ensure=True)
    rid = "req_keep_me"
    client.settings_write(
        confirmation="persistent-machine-settings",
        display="medium",
        request_id=rid,
    )
    assert calls == ["settings.write"]
    assert ensures == [1]
    # Second hardware RPC re-checks ensure; incompatibility aborts before RPC.
    with pytest.raises(Exception, match="incompatible|client-ready|protocol"):
        client.settings_write(
            confirmation="persistent-machine-settings",
            display="high",
            request_id="req_second",
        )
    assert ensures == [1, 1]
    assert calls == ["settings.write"]  # no second machine RPC


def test_bridge_error_category_loopback_contract(bridge_env):
    """Real BridgeServer JSON-line -> bridge_call preserves category; no token leak."""

    from xbloom_ble.bridge import bridge_record_path

    fake = FakeBridgeClient("AA:BB")

    async def boom_connect():
        fake.calls.append("connect")
        raise RuntimeError("BleakDeviceBusyError: already connected GATT busy")

    fake.connect = boom_connect  # type: ignore[method-assign]
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=bridge_env,
        client_factory=lambda _a: fake,
        machine_info_timeout=0.1,
    )
    server = BridgeServer(core, acquire_lock=False)
    record = bridge_record_path(bridge_env)

    async def go():
        task = asyncio.create_task(server.run())
        # Wait until bridge.json is published.
        for _ in range(100):
            if record.exists():
                break
            await asyncio.sleep(0.05)
        assert record.exists()
        data = json.loads(record.read_text(encoding="utf-8"))
        token = data.get("token")
        assert token

        def do_call():
            return bridge_call(
                "connect",
                {},
                require_hello=False,
                record_path=record,
                timeout=5.0,
            )

        with pytest.raises(BridgeError) as ei:
            await asyncio.to_thread(do_call)
        assert ei.value.category == "device_busy_external"
        assert token not in str(ei.value)
        assert "token" not in str(ei.value).casefold() or "device_busy" in str(ei.value)
        server.shutdown_event.set()
        await asyncio.wait_for(task, timeout=5.0)
        await core.shutdown(force=True)

    asyncio.run(go())


def test_probe_auto_releases_and_redacts(tmp_path):
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        machine_info_timeout=0.1,
    )

    async def go():
        result = await core.rpc("probe", {})
        assert result["brew_control_sent"] is False
        assert "serial_number" not in (result.get("machine_info") or {})
        assert result.get("machine_state_fresh") is True
        assert result.get("machine_state") == "idle"
        # Prompt-release runs in finally after the result dict is built.
        assert core.connected is False
        assert core.connection_scope is None
        assert "connect" in fake.calls
        assert "disconnect" in fake.calls
        # Explicit debug link retained across probe.
        await core.rpc("connect", {})
        await core.rpc("probe", {})
        assert core.connection_scope == "explicit"
        assert core.connected is True
        await core.rpc("disconnect")
        await core.shutdown()

    asyncio.run(go())


def test_probe_rejects_active_disconnected_without_connect(tmp_path):
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        machine_info_timeout=0.1,
    )
    recipe = _recipe(tmp_path / "r.yaml")

    async def go():
        await core.rpc(
            "coffee.load",
            {"recipe": str(recipe), "request_id": f"load_{uuid4().hex}"},
        )
        # Drop link while keeping durable workflow identity.
        if core.client is not None and hasattr(core.client, "drop_link"):
            core.client.drop_link()  # type: ignore[attr-defined]
        else:
            core.client = None
            core.connection_scope = None
        connects_before = fake.calls.count("connect")
        with pytest.raises(BridgeError, match="busy|active durable|recovery"):
            await core.rpc("probe", {})
        assert fake.calls.count("connect") == connects_before
        await core.shutdown(force=True)

    asyncio.run(go())


def test_probe_storage_error_fail_closed_no_connect(tmp_path):
    """A9: durable-state read failure must refuse connect (fail closed)."""
    from xbloom_storage import StorageError

    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        machine_info_timeout=0.1,
    )

    def boom_active():
        raise StorageError("injected durable state unreadable")

    core.store.get_active_workflow = boom_active  # type: ignore[method-assign]
    connects_before = fake.calls.count("connect")

    async def go():
        with pytest.raises(BridgeError, match="durable|unreadable") as ei:
            await core.rpc("probe", {})
        assert getattr(ei.value, "category", None) == "durable_state_unreadable"
        assert fake.calls.count("connect") == connects_before
        assert "connect" not in fake.calls
        await core.shutdown(force=True)

    asyncio.run(go())


def test_probe_marks_unconfirmed_when_no_fresh_state(tmp_path):
    fake = FakeBridgeClient("AA:BB")

    async def silent_request_status():
        fake.calls.append("request_status")
        # No state-bearing emit — fresh gate must time out.

    fake.request_status = silent_request_status  # type: ignore[method-assign]
    # Still provide machine_info on read.
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        machine_info_timeout=0.05,
    )

    async def go():
        # First connect needs machine_info for session; inject via open path.
        original_connect = fake.connect

        async def connect_and_info():
            await original_connect()
            fake.emit(
                _event(
                    command=40521,
                    name="machine_info",
                    machine_info=dict(fake.machine_info),
                )
            )

        fake.connect = connect_and_info  # type: ignore[method-assign]
        result = await core.rpc("probe", {})
        assert result.get("machine_state_fresh") is False
        assert result.get("machine_state_unconfirmed") is True
        assert result.get("machine_state") is None
        await core.shutdown(force=True)

    asyncio.run(go())


def test_load_start_same_workflow_no_per_step_connect(tmp_path):
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake,
        environ={
            "XBLOOM_ENABLE_REMOTE_START": "I_UNDERSTAND_REMOTE_HOT_WATER",
        },
        machine_info_timeout=0.1,
    )
    recipe = _recipe(tmp_path / "r.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load",
            {
                "recipe": str(recipe),
                "request_id": f"load_{uuid4().hex}",
            },
        )
        wid = loaded["workflow_id"]
        connects_after_load = fake.calls.count("connect")
        started = await core.rpc(
            "coffee.start",
            {
                "workflow_id": wid,
                "confirmation": "cup-filter-water-beans",
                "request_id": f"start_{uuid4().hex}",
            },
        )
        assert started["workflow_id"] == wid
        assert fake.calls.count("connect") == connects_after_load
        assert fake.calls.count("disconnect") == 0
        assert "coffee_start" in fake.calls
        await core.rpc(
            "cancel",
            {
                "workflow_id": wid,
                "request_id": f"cancel_{uuid4().hex}",
            },
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_cli_make_bridge_client_uses_typed_client(monkeypatch, tmp_path):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    client = xbloom.make_bridge_client(SimpleNamespace(address="AA:BB"))
    assert isinstance(client, TypedBridgeClient)


def test_no_direct_ble_commands_set():
    assert not hasattr(xbloom, "DIRECT_BLE_COMMANDS")
    assert not hasattr(xbloom, "ensure_bridge_not_running")


def test_no_five_minute_loaded_helpers():
    assert not hasattr(xbloom, "ARM_MAX_AGE_SECONDS")
    assert not hasattr(xbloom, "MAX_LOADED_AGE")


def test_cancel_does_not_silently_become_emergency(monkeypatch, tmp_path):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))
    calls = []

    class FakeTyped:
        def resolve_active_workflow_id(self, **k):
            raise BridgeError("no active workflow")

        def cancel(self, **kwargs):
            calls.append(kwargs)
            return {"status": "cancel_sent", **kwargs}

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: FakeTyped())
    monkeypatch.setattr(xbloom, "emit", lambda _d: None)
    args = xbloom.build_parser().parse_args(["cancel"])
    with pytest.raises(RuntimeError, match="--emergency|workflow"):
        asyncio.run(xbloom.async_cancel(args))
    assert calls == []

    args2 = xbloom.build_parser().parse_args(["cancel", "--emergency"])
    assert asyncio.run(xbloom.async_cancel(args2)) == 0
    assert calls and calls[0].get("emergency") is True


def test_cli_error_json_includes_category(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XBLOOM_STATE_DIR", str(tmp_path))

    class Boom:
        def probe(self, **k):
            raise BridgeError("device_busy_external: busy", category="device_busy_external")

    monkeypatch.setattr(xbloom, "make_bridge_client", lambda _a: Boom())
    monkeypatch.setattr(xbloom, "reexec_in_local_runtime", lambda: None)
    monkeypatch.setattr(xbloom, "require_runtime", lambda: None)
    rc = xbloom.main(["probe"])
    assert rc == 2
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["category"] == "device_busy_external"
    assert payload["type"] == "BridgeError"

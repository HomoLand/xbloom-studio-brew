"""Headless tests for the persistent bridge state machine and local transport."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from xbloom_ble import bridge as bridge_mod
from xbloom_ble.bridge import (
    ADVANCED_CONFIRM_SENTINEL,
    BridgeCore,
    BridgeError,
    BridgeServer,
    GRINDER_READY_SENTINEL,
    LIVE_ADJUST_ENV,
    LIVE_ADJUST_SENTINEL,
    READY_SENTINEL,
    REMOTE_GRINDER_ENV,
    REMOTE_GRINDER_SENTINEL,
    REMOTE_START_ENV,
    REMOTE_START_SENTINEL,
    SETTINGS_CONFIRM_SENTINEL,
    SETTINGS_WRITE_ENV,
    SETTINGS_WRITE_SENTINEL,
    TEA_READY_SENTINEL,
    WATER_READY_SENTINEL,
    bridge_call,
)
from xbloom_ble.client import XBloomError
from xbloom_ble.telemetry import StatusEvent
import xbloom_storage as storage


def _rid(prefix: str = "req") -> str:
    return f"{prefix}_{uuid4().hex}"


def _with_ids(
    params: dict | None = None,
    *,
    workflow_id: str | None = None,
    request_id: str | None = None,
    **extra,
) -> dict:
    body = dict(params or {})
    body.update(extra)
    body["request_id"] = request_id or _rid()
    if workflow_id is not None:
        body["workflow_id"] = workflow_id
    return body


def _event(
    *,
    command: int | None = None,
    state: int | None = None,
    name: str = "ack",
    machine_info: dict | None = None,
    water_ml: float | None = None,
    cup_g: float | None = None,
    report_name: str | None = None,
) -> StatusEvent:
    return StatusEvent(
        state=state,
        state_name=name,
        raw=b"test",
        command_code=command,
        report_name=report_name,
        machine_info=machine_info,
        water_g=water_ml,
        dispensed_water_ml=water_ml,
        coffee_g=cup_g,
        cup_weight_g=cup_g,
    )


class FakeBridgeClient:
    def __init__(self, address: str):
        self.address = address
        self.is_connected = False
        self.listeners = set()
        self.disconnect_listeners = set()
        self.calls = []
        self.fail_grinder_pause = False
        self.fail_coffee_start = False
        self.fail_coffee_load = False
        self.fail_tea_load = False
        self.fail_settings_writes = 0  # fail next N set_machine_settings calls
        self.fail_advanced_writes = 0
        self.fail_save_slots = False
        self.coffee_terminal_on_pause = False
        self.coffee_terminal_on_resume = False
        self.coffee_terminal_on_start = False
        self.fail_connect: Exception | None = None
        self.connect_count = 0
        self._expecting_disconnect = False
        # Futures blocked in machine ops; drop_link fails them with XBloomError
        # (mirrors production client transport-loss wake, not CancelledError).
        self._op_waiters: list[asyncio.Future] = []
        self.block_start_until_drop = False
        self.status_state: int | None = None
        self.status_state_name: str | None = None
        self.machine_info = {
            "serial_number": "private",
            "firmware": "V12.0D.500",
            "water_source": "tank",
            "weight_unit": "g",
            "temperature_unit": "C",
            "display": "medium",
            "pouring_radius_init": 680,
            "vibration_init": 1000,
        }
        self.advanced = {"pour_radius": 680, "vibration_amplitude": 1000}

    def add_event_listener(self, listener):
        self.listeners.add(listener)

    def remove_event_listener(self, listener):
        self.listeners.discard(listener)

    def add_disconnect_listener(self, listener):
        self.disconnect_listeners.add(listener)

    def remove_disconnect_listener(self, listener):
        self.disconnect_listeners.discard(listener)

    def mark_disconnect_expected(self):
        self._expecting_disconnect = True

    def emit(self, event: StatusEvent):
        for listener in tuple(self.listeners):
            listener(event)

    def _dispatch_disconnect(self, *, expected: bool):
        for listener in tuple(self.disconnect_listeners):
            listener(expected)

    def drop_link(self):
        """Simulate an unexpected BLE drop (not bridge-initiated)."""

        self.is_connected = False
        self.calls.append("drop_link")
        # Wake in-flight op waiters with a domain error (never Future.cancel).
        err = XBloomError("BLE link lost (unexpected disconnect)")
        for fut in list(self._op_waiters):
            if not fut.done():
                fut.set_exception(err)
        self._op_waiters.clear()
        self._dispatch_disconnect(expected=False)

    async def connect(self):
        self.connect_count += 1
        self.calls.append("connect")
        if self.fail_connect is not None:
            exc = self.fail_connect
            # One-shot by default so a later explicit op can succeed if cleared.
            raise exc
        self.is_connected = True
        self._expecting_disconnect = False

    async def disconnect(self):
        expected = bool(self._expecting_disconnect)
        self.is_connected = False
        self.calls.append("disconnect")
        err = XBloomError("BLE disconnected")
        for fut in list(self._op_waiters):
            if not fut.done():
                fut.set_exception(err)
        self._op_waiters.clear()
        # Bridge removes listeners before intentional disconnect; if still
        # registered, fire with expected so recovery is not invented.
        if self.disconnect_listeners:
            self._dispatch_disconnect(expected=expected or True)
        self._expecting_disconnect = False

    async def open_session(self):
        self.calls.append("open_session")

    async def close_session(self):
        self.calls.append("close_session")

    async def request_status(self):
        self.calls.append("request_status")
        if self.status_state is not None:
            self.emit(
                _event(
                    command=40521,
                    state=self.status_state,
                    name=self.status_state_name or "status",
                    machine_info=dict(self.machine_info),
                )
            )
        else:
            self.emit(
                _event(
                    command=40521,
                    name="machine_info",
                    machine_info=dict(self.machine_info),
                )
            )

    async def read_machine_info(self):
        self.calls.append("read_machine_info")
        return dict(self.machine_info)

    async def set_machine_settings(self, **requested):
        self.calls.append(("set_machine_settings", dict(requested)))
        if self.fail_settings_writes > 0:
            self.fail_settings_writes -= 1
            raise RuntimeError("settings write acknowledgement lost")
        self.machine_info.update(requested)
        return dict(self.machine_info)

    async def read_advanced_settings(self):
        self.calls.append("read_advanced_settings")
        return dict(self.advanced)

    async def write_advanced_settings(self, **requested):
        self.calls.append(("write_advanced_settings", dict(requested)))
        if self.fail_advanced_writes > 0:
            self.fail_advanced_writes -= 1
            raise RuntimeError("advanced write acknowledgement lost")
        self.advanced.update(
            {key: value for key, value in requested.items() if value is not None}
        )
        return dict(self.advanced)

    async def load_recipe(self, recipe):
        self.calls.append(("load_recipe", recipe.name))
        if self.fail_coffee_load:
            raise RuntimeError("load acknowledgement lost")
        event = _event(state=0x1F, name="armed")
        self.emit(event)
        return event

    async def start(self):
        self.calls.append("coffee_start")
        if self.fail_coffee_start:
            raise RuntimeError("start acknowledgement lost")
        if self.block_start_until_drop:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._op_waiters.append(fut)
            # Transport loss (drop_link) fails this future with XBloomError.
            return await fut
        if self.coffee_terminal_on_start:
            # Active then terminal while start is in-flight (before return).
            self.emit(_event(state=0x22, name="starting"))
            self.emit(_event(state=0x24, name="ready"))
            return _event(state=0x24, name="ready")
        event = _event(state=0x22, name="starting")
        self.emit(event)
        return event

    async def pause_coffee(self):
        self.calls.append("coffee_pause")
        if self.coffee_terminal_on_pause:
            self.emit(_event(state=0x24, name="ready"))
        return _event(command=40518)

    async def resume_coffee(self):
        self.calls.append("coffee_resume")
        if self.coffee_terminal_on_resume:
            self.emit(_event(state=0x24, name="ready"))
            return _event(command=40524, state=0x24, name="ready")
        return _event(command=40524)

    async def cancel_brew(self):
        self.calls.append("coffee_cancel")

    async def load_tea_recipe(self, recipe):
        self.calls.append(("tea_load", recipe.name))
        if self.fail_tea_load:
            raise RuntimeError("tea load acknowledgement lost")
        event = _event(command=4513, name="tea_recipe_code")
        self.emit(event)
        return event

    async def start_tea(self):
        self.calls.append("tea_start")
        event = _event(command=4512, name="tea_recipe_make")
        self.emit(event)
        return event

    async def unload_tea_recipe(self):
        self.calls.append("tea_unload")

    async def stream_scale(self, on_event, *, duration, tare, on_ready):
        self.calls.append(("scale_start", duration, tare))
        await on_ready()
        event = _event(name="scale", report_name="scale_weight")
        event.scale_g = 12.34
        self.emit(event)
        await on_event(event)
        await asyncio.sleep(duration)

    async def tare_scale(self):
        self.calls.append("scale_tare")

    async def start_grinder_session(self, size, rpm):
        self.calls.append(("grinder_start", size, rpm))
        return _event(command=3500)

    async def pause_grinder(self):
        self.calls.append("grinder_pause")
        if self.fail_grinder_pause:
            raise RuntimeError("simulated pause ACK loss")
        return _event(command=8018)

    async def resume_grinder(self):
        self.calls.append("grinder_resume")
        return _event(command=8020)

    async def stop_grinder_session(self):
        self.calls.append("grinder_stop")
        await asyncio.sleep(0)
        return _event(command=3505)

    async def start_water_session(self, volume, temp, **kwargs):
        self.calls.append(("water_start", volume, temp, kwargs))

    async def pause_water(self):
        self.calls.append("water_pause")
        return _event(command=8019)

    async def resume_water(self):
        self.calls.append("water_resume")
        return _event(command=8021)

    async def set_water_temperature(self, temp):
        self.calls.append(("water_temperature", temp))
        return _event(command=8108)

    async def set_water_pattern(self, pattern):
        self.calls.append(("water_pattern", pattern))
        return _event(command=8107)

    async def stop_water_session(self):
        self.calls.append("water_stop")
        event = _event(command=4507, name="brewer_stop_echo")
        self.emit(event)
        return event

    async def quit_water_session(self):
        self.calls.append("water_quit")

    async def save_slots(self, recipes, *, scale=True):
        self.calls.append(("save_slots", [recipe.name for recipe in recipes], scale))
        if self.fail_save_slots:
            raise RuntimeError("slot write acknowledgement lost")


def _environment(
    *,
    live_adjust: bool = False,
    settings_write: bool = False,
    idle_disconnect_s: float | None = None,
) -> dict[str, str]:
    values = {
        REMOTE_START_ENV: REMOTE_START_SENTINEL,
        REMOTE_GRINDER_ENV: REMOTE_GRINDER_SENTINEL,
    }
    if live_adjust:
        values[LIVE_ADJUST_ENV] = LIVE_ADJUST_SENTINEL
    if settings_write:
        values[SETTINGS_WRITE_ENV] = SETTINGS_WRITE_SENTINEL
    if idle_disconnect_s is not None:
        values["XBLOOM_BRIDGE_IDLE_DISCONNECT_S"] = str(idle_disconnect_s)
    return values


def _core(
    tmp_path: Path,
    *,
    live_adjust: bool = False,
    settings_write: bool = False,
    idle_disconnect_s: float | None = None,
):
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _address: fake,
        environ=_environment(
            live_adjust=live_adjust,
            settings_write=settings_write,
            idle_disconnect_s=idle_disconnect_s,
        ),
        machine_info_timeout=0.1,
    )
    return core, fake


def _recipe(path: Path) -> Path:
    path.write_text(
        """name: Bridge test
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


def _tea_recipe(path: Path) -> Path:
    path.write_text(
        """name: Bridge tea test
kind: tea
leaf_g: 4
output_ml_per_steep: 100
pours:
  - {ml: 80, temp_c: 90, pattern: circular, pause_s: 20, flow_ml_s: 3.5}
  - {ml: 80, temp_c: 90, pattern: circular, pause_s: 20, flow_ml_s: 3.5}
""",
        encoding="utf-8",
    )
    return path


def _stored_coffee_content(name: str = "Stored coffee") -> dict:
    return {
        "name": name,
        "kind": "hot",
        "dose_g": 16,
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
                "ml": 100,
                "temp_c": 92,
                "pattern": "spiral",
                "pause_s": 5,
                "rpm": 100,
                "flow_ml_s": 3.0,
            },
            {
                "ml": 100,
                "temp_c": 92,
                "pattern": "spiral",
                "pause_s": 5,
                "rpm": 100,
                "flow_ml_s": 3.0,
            },
        ],
    }


def _stored_tea_content(name: str = "Stored tea") -> dict:
    return {
        "name": name,
        "kind": "tea",
        "leaf_g": 4,
        "output_ml_per_steep": 100,
        "pours": [
            {
                "ml": 80,
                "temp_c": 90,
                "pattern": "circular",
                "pause_s": 20,
                "flow_ml_s": 3.5,
            },
            {
                "ml": 80,
                "temp_c": 90,
                "pattern": "circular",
                "pause_s": 20,
                "flow_ml_s": 3.5,
            },
        ],
    }


def _string_values(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _string_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _string_values(child)
    elif isinstance(value, str):
        yield value


def test_revision_only_coffee_and_tea_load_and_start_without_paths(tmp_path):
    core, fake = _core(tmp_path)
    coffee = core.store.create_recipe_with_revision(_stored_coffee_content())
    tea = core.store.create_recipe_with_revision(_stored_tea_content())
    coffee_revision = coffee["revision"]
    tea_revision = tea["revision"]

    async def go():
        loaded_coffee = await core.rpc(
            "coffee.load",
            _with_ids(
                {"recipe_revision_id": coffee_revision["revision_id"]}
            ),
        )
        coffee_workflow = core.store.get_workflow(loaded_coffee["workflow_id"])
        assert not core.coffee_state_file.exists()
        assert loaded_coffee["recipe_revision_id"] == coffee_revision["revision_id"]
        assert coffee_workflow["recipe_revision_id"] == coffee_revision["revision_id"]
        assert coffee_workflow["snapshot"] == coffee_revision["content"]
        assert coffee_workflow["snapshot_sha256"] == coffee_revision["content_sha256"]
        assert all(
            str(tmp_path) not in value
            for value in _string_values(
                {
                    "result": loaded_coffee,
                    "workflow": coffee_workflow,
                }
            )
        )
        assert fake.connect_count == 1
        assert fake.calls.count(("load_recipe", "Stored coffee")) == 1

        await core.rpc(
            "cancel", _with_ids(workflow_id=loaded_coffee["workflow_id"])
        )
        await _drain_release(core)

        loaded_tea = await core.rpc(
            "tea.load",
            _with_ids({"recipe_revision_id": tea_revision["revision_id"]}),
        )
        tea_workflow = core.store.get_workflow(loaded_tea["workflow_id"])
        assert not core.tea_state_file.exists()
        assert loaded_tea["recipe_revision_id"] == tea_revision["revision_id"]
        assert tea_workflow["recipe_revision_id"] == tea_revision["revision_id"]
        assert tea_workflow["snapshot"] == tea_revision["content"]
        assert tea_workflow["snapshot_sha256"] == tea_revision["content_sha256"]
        assert all(
            str(tmp_path) not in value
            for value in _string_values(
                {
                    "result": loaded_tea,
                    "workflow": tea_workflow,
                }
            )
        )
        assert fake.connect_count == 2
        assert fake.calls.count(("tea_load", "Stored tea")) == 1

        await core.rpc(
            "tea.start",
            _with_ids(
                {"confirmation": TEA_READY_SENTINEL},
                workflow_id=loaded_tea["workflow_id"],
            ),
        )
        assert fake.connect_count == 2
        assert fake.calls.count("tea_start") == 1
        await core.rpc(
            "cancel", _with_ids(workflow_id=loaded_tea["workflow_id"])
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_revision_only_rejects_unknown_malformed_and_cross_kind_pre_ble(tmp_path):
    core, fake = _core(tmp_path)
    coffee = core.store.create_recipe_with_revision(_stored_coffee_content())
    tea = core.store.create_recipe_with_revision(_stored_tea_content())

    bad_coffee = core.store.upsert_recipe(
        recipe_id="rcp_bad_coffee", kind="coffee", name="Bad coffee"
    )
    bad_coffee_revision = core.store.add_recipe_revision(
        bad_coffee["recipe_id"], {"name": "Bad coffee", "pours": []}
    )
    bad_tea = core.store.upsert_recipe(
        recipe_id="rcp_bad_tea", kind="tea", name="Bad tea"
    )
    bad_tea_revision = core.store.add_recipe_revision(
        bad_tea["recipe_id"], {"name": "Bad tea", "kind": "tea", "pours": []}
    )

    async def go():
        cases = (
            ("coffee.load", "rev_missing", "unknown"),
            ("coffee.load", tea["revision"]["revision_id"], "does not match"),
            ("tea.load", coffee["revision"]["revision_id"], "does not match"),
            (
                "coffee.load",
                bad_coffee_revision["revision_id"],
                "validation",
            ),
            ("tea.load", bad_tea_revision["revision_id"], "validation"),
        )
        for method, revision_id, message in cases:
            with pytest.raises(BridgeError, match=message) as exc_info:
                await core.rpc(
                    method,
                    _with_ids({"recipe_revision_id": revision_id}),
                )
            assert exc_info.value.category in {"invalid_request", "validation_error"}
            assert core.store.get_active_workflow() is None
        assert fake.connect_count == 0
        assert "connect" not in fake.calls
        assert not any(
            isinstance(call, tuple) and call[0] in {"load_recipe", "tea_load"}
            for call in fake.calls
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_coffee_lifecycle_uses_one_held_client(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        connected = await core.rpc("connect")
        assert connected["connected"] is True
        assert "serial_number" not in core.machine_info

        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        assert loaded["status"] == "armed"
        assert loaded["workflow_id"]
        assert not core.coffee_state_file.exists()
        wid = loaded["workflow_id"]

        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert core.status()["phase"] == "running"
        await core.rpc("pause", _with_ids(workflow_id=wid))
        assert core.status()["phase"] == "paused"
        await core.rpc("resume", _with_ids(workflow_id=wid))
        assert core.status()["phase"] == "running"
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        assert core.status()["activity"] is None
        assert not core.coffee_state_file.exists()
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 1
    assert "coffee_pause" in fake.calls and "coffee_resume" in fake.calls


def test_coffee_start_failure_requires_recovery_instead_of_retry(tmp_path):
    core, fake = _core(tmp_path)
    fake.fail_coffee_start = True
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        start_req = _rid("start")
        with pytest.raises(BridgeError, match="do not retry start"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )

        status = core.status()
        assert status["activity"] == "coffee"
        assert status["phase"] == "control_unconfirmed"
        # Unconfirmed start is durable-only; no coffee JSON dual-write.
        assert not core.coffee_state_file.exists()
        wf = core.store.get_workflow(wid)
        assert wf is not None
        assert wf["state"] == "control_unconfirmed"

        # Same pending request_id must not reissue start.
        with pytest.raises(BridgeError, match="recovery_required|do not retry"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )
        assert fake.calls.count("coffee_start") == 1
        # Fresh request_id while phase is not loaded is also rejected (no BLE).
        with pytest.raises(BridgeError, match="no loaded coffee recipe"):
            await core.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert fake.calls.count("coffee_start") == 1

        await core.rpc("cancel", _with_ids(workflow_id=wid))
        assert not core.coffee_state_file.exists()
        await core.shutdown()

    asyncio.run(go())


def test_coffee_terminal_during_pause_does_not_restore_stale_paused_state(tmp_path):
    core, fake = _core(tmp_path)
    fake.coffee_terminal_on_pause = True
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        result = await core.rpc("pause", _with_ids(workflow_id=wid))
        assert result["terminal_during_control"] is True
        assert core.status()["activity"] is None
        assert core.status()["phase"] == "idle"
        assert not core.coffee_state_file.exists()
        await core.shutdown()

    asyncio.run(go())


def test_stale_legacy_json_ignored_without_migration_or_cancel(tmp_path):
    """Unmigrated armed-state/tea-loaded JSON is not a runtime gate or cancel source."""

    core, fake = _core(tmp_path)
    core.coffee_state_file.write_text(
        json.dumps(
            {
                "address": "AA:BB",
                "status": "completion_unconfirmed",
                "owner": "bridge",
            }
        ),
        encoding="utf-8",
    )
    core.tea_state_file.write_text(
        json.dumps({"address": "AA:BB", "status": "tea_loaded"}),
        encoding="utf-8",
    )
    original_coffee = core.coffee_state_file.read_bytes()
    original_tea = core.tea_state_file.read_bytes()

    async def go():
        # settings.read must proceed; legacy files are not ownership gates.
        await core.rpc("settings.read")
        assert fake.connect_count == 1
        with pytest.raises(BridgeError, match="no bridge-owned activity"):
            await core.rpc("cancel", _with_ids())
        # Originals untouched (import-only; runtime never unlinks).
        assert core.coffee_state_file.read_bytes() == original_coffee
        assert core.tea_state_file.read_bytes() == original_tea
        await core.shutdown(force=True)

    asyncio.run(go())


def test_tea_lifecycle_stays_on_held_connection_and_finishes_on_terminal(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        assert loaded["status"] == "tea_loaded"
        assert loaded["workflow_id"]
        assert not core.tea_state_file.exists()
        wid = loaded["workflow_id"]

        started = await core.rpc(
            "tea.start",
            _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
        )
        assert started["ack"] == 4512
        fake.emit(
            _event(
                command=9012,
                name="tea_soaking",
                report_name="tea_soaking",
            )
        )
        assert core.status()["phase"] == "soaking"
        fake.emit(_event(state=0x01, name="idle"))
        assert core.status()["activity"] is None
        assert core.status()["last_operation"]["activity"] == "tea"
        assert not core.tea_state_file.exists()
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 1
    assert ("tea_load", "Bridge tea test") in fake.calls
    assert "tea_start" in fake.calls


def test_scale_runs_in_background_supports_retare_and_cancel(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        started = await core.rpc(
            "scale.start", _with_ids({"duration_s": 10, "tare": False})
        )
        assert started["entry_auto_zero"] is True
        assert started["workflow_id"]
        wid = started["workflow_id"]
        await asyncio.sleep(0)
        assert core.status()["activity"] == "scale"
        assert core.status()["telemetry"]["scale_g"] == 12.34
        tare = await core.rpc("scale.tare", _with_ids(workflow_id=wid))
        assert tare["command_write_verified"] is True
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        assert core.status()["activity"] is None
        assert core.status()["last_operation"]["result"] == "stopped"
        await core.shutdown()

    asyncio.run(go())
    assert ("scale_start", 10.0, False) in fake.calls
    assert "scale_tare" in fake.calls


def test_settings_advanced_and_presets_share_bridge_owner(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    recipes = [
        _recipe(tmp_path / f"recipe-{slot}.yaml") for slot in "abc"
    ]

    async def go():
        settings = await core.rpc("settings.read")
        assert settings["settings"]["display"] == "medium"
        assert core.connected is False  # read-only one-shot released
        written = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        assert written["readback"] == {"display": "high"}
        assert written["workflow_id"]
        await _drain_release(core)
        assert core.connected is False

        advanced = await core.rpc("advanced.read")
        assert advanced["settings"]["pour_radius_level"] == 3
        assert core.connected is False
        tuned = await core.rpc(
            "advanced.write",
            _with_ids(
                {
                    "pour_radius_level": 4,
                    "vibration_level": 2,
                    "confirmation": ADVANCED_CONFIRM_SENTINEL,
                }
            ),
        )
        assert tuned["readback"]["pour_radius"] == 760
        assert tuned["readback"]["vibration_amplitude"] == 1100
        await _drain_release(core)
        assert core.connected is False

        saved = await core.rpc(
            "presets.save",
            _with_ids({"recipes": [str(path) for path in recipes]}),
        )
        assert saved["status"] == "saved"
        assert saved["brew_started"] is False
        assert saved["workflow_id"]
        await _drain_release(core)
        assert core.connected is False
        await core.shutdown()

    asyncio.run(go())
    # Each one-shot releases, so each hardware op reconnects.
    assert fake.calls.count("connect") >= 5
    assert any(call[0] == "set_machine_settings" for call in fake.calls if isinstance(call, tuple))
    assert any(call[0] == "write_advanced_settings" for call in fake.calls if isinstance(call, tuple))
    assert any(call[0] == "save_slots" for call in fake.calls if isinstance(call, tuple))


def test_bridge_persistent_writes_keep_their_independent_gate(tmp_path):
    core, _fake = _core(tmp_path, settings_write=False)

    async def go():
        with pytest.raises(BridgeError, match="persistent machine writes disabled"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    }
                ),
            )

    asyncio.run(go())


def test_local_validation_and_recovery_records_block_before_ble_connect(tmp_path):
    core, fake = _core(tmp_path)
    # Stale legacy JSON is ignored; plant durable active ownership instead.
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        with pytest.raises(BridgeError, match="owns|active durable workflow"):
            await core.rpc("settings.read")
        assert fake.calls.count("connect") == 1
        await core.rpc("cancel", _with_ids(workflow_id=loaded["workflow_id"]))
        await _drain_release(core)
        with pytest.raises(BridgeError, match="volume must be 20-360"):
            await core.rpc(
                "water.start",
                _with_ids(
                    {
                        "volume_ml": 500,
                        "temp_c": 85,
                        "confirmation": WATER_READY_SENTINEL,
                    }
                ),
            )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_freesolo_water_live_adjust_is_separately_gated(tmp_path):
    core, _fake = _core(tmp_path, live_adjust=False)

    async def blocked():
        started = await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 20,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        wid = started["workflow_id"]
        with pytest.raises(BridgeError, match="not hardware A/B verified"):
            await core.rpc(
                "water.set_pattern",
                _with_ids(
                    {"pattern": "spiral", "confirmation": LIVE_ADJUST_SENTINEL},
                    workflow_id=wid,
                ),
            )
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(blocked())

    enabled, fake = _core(tmp_path / "enabled", live_adjust=True)

    async def allowed():
        started = await enabled.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 20,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        wid = started["workflow_id"]
        await enabled.rpc("pause", _with_ids(workflow_id=wid))
        temperature = await enabled.rpc(
            "water.set_temperature",
            _with_ids(
                {"temp_c": 60, "confirmation": LIVE_ADJUST_SENTINEL},
                workflow_id=wid,
            ),
        )
        pattern = await enabled.rpc(
            "water.set_pattern",
            _with_ids(
                {"pattern": "spiral", "confirmation": LIVE_ADJUST_SENTINEL},
                workflow_id=wid,
            ),
        )
        assert not temperature["hardware_effect_verified"]
        assert pattern["hardware_effect_verified"]
        assert pattern["report"] == 8107
        await enabled.rpc("resume", _with_ids(workflow_id=wid))
        await enabled.rpc("cancel", _with_ids(workflow_id=wid))
        await enabled.shutdown()

    asyncio.run(allowed())
    assert ("water_temperature", 60) in fake.calls
    assert ("water_pattern", "spiral") in fake.calls


def test_grinder_pause_extends_timer_and_stop_persists_cooldown(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        started = await core.rpc(
            "grinder.start",
            _with_ids(
                {
                    "size": 60,
                    "rpm": 100,
                    "seconds": 0.15,
                    "confirmation": GRINDER_READY_SENTINEL,
                }
            ),
        )
        wid = started["workflow_id"]
        await asyncio.sleep(0.03)
        await core.rpc("pause", _with_ids(workflow_id=wid))
        await asyncio.sleep(0.18)
        assert core.status()["phase"] == "paused"
        await core.rpc("resume", _with_ids(workflow_id=wid))
        await asyncio.sleep(0.16)
        assert core.status()["activity"] is None
        record = core.grinder_state_file.read_text(encoding="utf-8")
        assert '"in_progress": false' in record
        await core.shutdown()

    asyncio.run(go())
    assert "grinder_pause" in fake.calls and "grinder_resume" in fake.calls
    assert "grinder_stop" in fake.calls


def test_grinder_pause_ack_loss_forces_confirmed_stop(tmp_path):
    core, fake = _core(tmp_path)
    fake.fail_grinder_pause = True

    async def go():
        started = await core.rpc(
            "grinder.start",
            _with_ids(
                {
                    "size": 60,
                    "rpm": 100,
                    "seconds": 10,
                    "confirmation": GRINDER_READY_SENTINEL,
                }
            ),
        )
        wid = started["workflow_id"]
        with pytest.raises(BridgeError, match="STOP/QUIT was confirmed"):
            await core.rpc("pause", _with_ids(workflow_id=wid))
        assert core.status()["activity"] is None
        assert core.status()["last_operation"]["result"] == "pause_failed_stopped"
        assert '"in_progress": false' in core.grinder_state_file.read_text(
            encoding="utf-8"
        )
        await core.shutdown()

    asyncio.run(go())
    assert "grinder_stop" in fake.calls


def test_natural_water_stop_requires_metered_target(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 85,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        fake.emit(_event(command=40523, name="water_volume", water_ml=70.0))
        fake.emit(_event(command=40511, name="brewer_stopped"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status = core.status()
        assert status["activity"] is None
        assert status["last_operation"]["result"] == "completion_unconfirmed"
        assert status["last_operation"]["metered_volume_ml"] == 70.0
        assert "stopped early" in status["last_error"]
        await core.shutdown()

    asyncio.run(go())


def test_natural_water_stop_uses_peak_before_firmware_meter_reset(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 20,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        fake.emit(_event(command=40523, name="water_volume", water_ml=100.7))
        fake.emit(_event(command=40511, name="brewer_stopped"))
        fake.emit(_event(command=40523, name="water_volume", water_ml=0.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status = core.status()
        assert status["activity"] is None
        assert status["last_operation"]["result"] == "complete"
        assert status["last_operation"]["metered_volume_ml"] == 100.7
        assert status["telemetry"]["water_ml"] == 0.0
        assert status["telemetry"]["water_peak_ml"] == 100.7
        await core.shutdown()

    asyncio.run(go())


def test_water_status_separates_target_dispensed_and_cup_scale_delta(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 85,
                    "pattern": "circular",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        fake.emit(_event(cup_g=30.0, name="scale"))
        fake.emit(_event(command=40523, name="water_volume", water_ml=55.0))
        fake.emit(_event(cup_g=82.5, name="scale"))
        status = core.status()
        assert status["liquid_progress"] == {
            "target_dispensed_water_ml": 100.0,
            "dispensed_water_ml": 55.0,
            "remaining_ml": 45.0,
            "dispensed_vs_target_ml": -45.0,
            "cup_delta_g": 52.5,
        }
        assert status["telemetry"]["cup_weight_g"] == 82.5

        fake.emit(_event(command=40523, name="water_volume", water_ml=100.0))
        fake.emit(_event(command=40511, name="brewer_stopped"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        final = core.status()["last_operation"]
        assert final["target_dispensed_water_ml"] == 100.0
        assert final["dispensed_water_ml"] == 100.0
        assert final["cup_delta_g"] == 52.5
        await core.shutdown()

    asyncio.run(go())


def test_water_peak_survives_firmware_meter_reset_after_explicit_stop(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        started = await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 200,
                    "temp_c": 20,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        wid = started["workflow_id"]
        fake.emit(_event(command=40523, name="water_volume", water_ml=97.65))
        fake.emit(_event(command=40523, name="water_volume", water_ml=0.0))
        result = await core.rpc("cancel", _with_ids(workflow_id=wid))
        assert result["ack"] == 4507
        status = core.status()
        assert status["last_operation"]["metered_volume_ml"] == 97.65
        assert status["telemetry"]["water_ml"] == 0.0
        assert status["telemetry"]["water_peak_ml"] == 97.65
        await core.shutdown()

    asyncio.run(go())


def test_water_safety_timer_stops_without_cancelling_its_own_cleanup(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 85,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        core._start_water_timer(0.01)
        await asyncio.sleep(0.03)
        assert core.status()["activity"] is None
        assert core.status()["last_operation"]["result"] == "safety_timeout_stopped"
        await core.shutdown()

    asyncio.run(go())
    assert "water_stop" in fake.calls


def test_loopback_json_transport_round_trip(tmp_path):
    record = tmp_path / "bridge.json"
    core = BridgeCore(state_dir=tmp_path, environ={})
    server = BridgeServer(core, record_path=record, token="test-token")

    async def go():
        task = asyncio.create_task(server.run())
        for _ in range(100):
            if record.exists():
                break
            await asyncio.sleep(0.01)
        assert record.exists()
        status = await asyncio.to_thread(
            bridge_call, "status", record_path=record, timeout=2.0
        )
        assert status["running"] is True
        assert "token" not in status
        result = await asyncio.to_thread(
            bridge_call, "shutdown", record_path=record, timeout=2.0
        )
        assert result["status"] == "shutting_down"
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(go())
    assert not record.exists()


def test_bridge_client_rejects_non_loopback_record(tmp_path):
    record = tmp_path / "bridge.json"
    record.write_text(
        json.dumps({"host": "192.0.2.10", "port": 1234, "token": "x"}),
        encoding="utf-8",
    )
    with pytest.raises(BridgeError, match="required loopback host"):
        bridge_call("status", record_path=record, timeout=0.1)


async def _drain_release(core: BridgeCore, *, timeout: float = 1.0) -> None:
    """Wait for a scheduled prompt BLE release to finish."""

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        task = core._release_task
        if not core.release_pending and (task is None or task.done()):
            return
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            continue
        await asyncio.sleep(0)
    task = core._release_task
    if task is not None and not task.done():
        await asyncio.wait_for(task, timeout=0.2)


def test_daemon_construction_and_status_do_not_connect(tmp_path):
    core, fake = _core(tmp_path)
    status = core.status()
    assert status["running"] is True
    assert status["connected"] is False
    assert status["connection_scope"] is None
    assert status["release_pending"] is False
    assert status["last_disconnect_reason"] is None
    assert fake.calls == []

    async def go():
        polled = await core.rpc("status")
        assert polled["connected"] is False
        events = await core.rpc("events", {"since": 0})
        assert events["events"] == []
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls == []


def test_coffee_workflow_connects_once_and_releases_on_natural_terminal(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        assert loaded["status"] == "armed"
        wid = loaded["workflow_id"]
        assert core.status()["connection_scope"] == "workflow"
        assert core.status()["active_workflow_id"] == wid
        assert fake.calls.count("connect") == 1
        assert fake.calls.count("open_session") == 1

        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        await core.rpc("pause", _with_ids(workflow_id=wid))
        assert core.status()["phase"] == "paused"
        await core.rpc("resume", _with_ids(workflow_id=wid))
        assert core.status()["phase"] == "running"
        # status/events must not reconnect or extend the link
        await core.rpc("status")
        await core.rpc("events", {"since": 0})
        await core.rpc("events", {"since": 0, "workflow_id": wid})
        assert fake.calls.count("connect") == 1

        fake.emit(_event(state=0x24, name="ready"))
        await _drain_release(core)
        status = core.status()
        assert status["activity"] is None
        assert status["connected"] is False
        assert status["connection_scope"] is None
        assert status["running"] is True
        assert status["last_operation"]["result"] == "ready"
        assert status["last_disconnect_reason"] == "natural_terminal"
        assert status["last_disconnect_error"] is None
        assert not core.coffee_state_file.exists()
        # Durable terminal committed before release.
        wf = core.store.get_workflow(wid)
        assert wf is not None
        assert wf["terminal_at"] is not None

        # Next workflow can reconnect once the daemon remains up.
        loaded2 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        assert core.status()["connected"] is True
        assert fake.calls.count("connect") == 2
        await core.rpc(
            "cancel", _with_ids(workflow_id=loaded2["workflow_id"])
        )
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 2
    assert fake.calls.count("open_session") == 2
    assert fake.calls.count("close_session") == 2
    assert fake.calls.count("disconnect") == 2
    assert "coffee_pause" in fake.calls and "coffee_resume" in fake.calls


def test_tea_workflow_releases_once_on_natural_terminal(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "tea.start",
            _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
        )
        await core.rpc("status")
        fake.emit(_event(state=0x01, name="idle"))
        await _drain_release(core)
        status = core.status()
        assert status["connected"] is False
        assert status["last_operation"]["activity"] == "tea"
        assert status["last_disconnect_reason"] == "natural_terminal"
        assert status["running"] is True
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 1
    assert fake.calls.count("open_session") == 1
    assert fake.calls.count("close_session") == 1
    assert fake.calls.count("disconnect") == 1


def test_explicit_cancel_stop_releases_once_for_each_activity(tmp_path):
    recipe = _recipe(tmp_path / "recipe.yaml")
    tea = _tea_recipe(tmp_path / "tea.yaml")

    async def coffee_cancel():
        core, fake = _core(tmp_path / "coffee")
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core)
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] == "cancel"
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()
        return fake

    async def tea_cancel():
        core, fake = _core(tmp_path / "tea")
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(tea)})
        )
        await core.rpc("cancel", _with_ids(workflow_id=loaded["workflow_id"]))
        await _drain_release(core)
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] == "cancel"
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()
        return fake

    async def grinder_stop():
        core, fake = _core(tmp_path / "grinder")
        started = await core.rpc(
            "grinder.start",
            _with_ids(
                {
                    "size": 60,
                    "rpm": 100,
                    "seconds": 10,
                    "confirmation": GRINDER_READY_SENTINEL,
                }
            ),
        )
        await core.rpc(
            "cancel", _with_ids(workflow_id=started["workflow_id"])
        )
        await _drain_release(core)
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] == "grinder_confirmed_stop"
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()
        return fake

    async def water_stop():
        core, fake = _core(tmp_path / "water")
        started = await core.rpc(
            "water.start",
            _with_ids(
                {
                    "volume_ml": 100,
                    "temp_c": 85,
                    "pattern": "center",
                    "water_source": "tank",
                    "confirmation": WATER_READY_SENTINEL,
                }
            ),
        )
        await core.rpc(
            "cancel", _with_ids(workflow_id=started["workflow_id"])
        )
        await _drain_release(core)
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] == "water_confirmed_stop"
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()
        return fake

    async def scale_stop():
        core, fake = _core(tmp_path / "scale")
        started = await core.rpc(
            "scale.start", _with_ids({"duration_s": 10, "tare": False})
        )
        await core.rpc(
            "cancel", _with_ids(workflow_id=started["workflow_id"])
        )
        await _drain_release(core)
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] in {
            "scale_stopped",
            "scale_complete",
        }
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()
        return fake

    asyncio.run(coffee_cancel())
    asyncio.run(tea_cancel())
    asyncio.run(grinder_stop())
    asyncio.run(water_stop())
    asyncio.run(scale_stop())


def test_explicit_connect_does_not_auto_release(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        await core.rpc("connect")
        assert core.status()["connection_scope"] == "explicit"
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        assert core.status()["connection_scope"] == "explicit"
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core)
        status = core.status()
        assert status["connected"] is True
        assert status["connection_scope"] == "explicit"
        assert status["last_disconnect_reason"] is None
        assert fake.calls.count("disconnect") == 0
        await core.rpc("disconnect")
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] == "explicit"
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 1
    assert fake.calls.count("disconnect") == 1


def test_loaded_recipe_holds_without_timeout_then_start_reuses_connection(tmp_path):
    """Loaded coffee waits for start; no time-driven unload; one link until terminal."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        assert core.status()["phase"] == "loaded"
        assert core.status()["connected"] is True
        assert core.status()["connection_scope"] == "workflow"
        # No arm-expiry / loaded-timeout machinery.
        assert not hasattr(core, "_arm_expiry_task")
        assert not hasattr(core, "_arm_expiry_key")
        assert not hasattr(bridge_mod, "ARM_MAX_AGE_SECONDS")
        assert not core.coffee_state_file.exists()

        # Wait beyond any historical five-minute window; no auto-cancel.
        await asyncio.sleep(0.1)
        status = core.status()
        assert status["phase"] == "loaded"
        assert status["activity"] == "coffee"
        assert status["connected"] is True
        assert status["running"] is True
        assert fake.calls.count("connect") == 1
        assert fake.calls.count("disconnect") == 0
        assert "coffee_cancel" not in fake.calls

        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert core.status()["phase"] == "running"
        assert fake.calls.count("connect") == 1
        assert not core.coffee_state_file.exists()

        await core.rpc("pause", _with_ids(workflow_id=wid))
        await core.rpc("resume", _with_ids(workflow_id=wid))
        await core.rpc("status")
        await core.rpc("events", {"since": 0})
        assert fake.calls.count("connect") == 1

        fake.emit(_event(state=0x24, name="ready"))
        await _drain_release(core)
        status = core.status()
        assert status["activity"] is None
        assert status["connected"] is False
        assert status["connection_scope"] is None
        assert status["running"] is True
        assert status["last_operation"]["result"] == "ready"
        assert status["last_disconnect_reason"] == "natural_terminal"
        assert not core.coffee_state_file.exists()
        assert fake.calls.count("connect") == 1
        assert fake.calls.count("open_session") == 1
        assert fake.calls.count("close_session") == 1
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()

    asyncio.run(go())


def test_preflight_failure_releases_auto_connect_keeps_explicit(tmp_path):
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def auto_connect_cleanup():
        core, fake = _core(tmp_path / "auto")
        fake.machine_info["firmware"] = "UNSUPPORTED.1"
        with pytest.raises(BridgeError, match="not in the tested set"):
            await core.rpc(
                "coffee.load", _with_ids({"recipe": str(recipe)})
            )
        await _drain_release(core)
        assert core.status()["connected"] is False
        assert core.status()["last_disconnect_reason"] == "preflight_or_load_failed"
        assert fake.calls.count("connect") == 1
        assert fake.calls.count("disconnect") == 1
        await core.shutdown()

    async def explicit_kept():
        core, fake = _core(tmp_path / "explicit")
        await core.rpc("connect")
        fake.machine_info["firmware"] = "UNSUPPORTED.1"
        # machine_info already cached from connect; force untested firmware gate
        core.machine_info["firmware"] = "UNSUPPORTED.1"
        with pytest.raises(BridgeError, match="not in the tested set"):
            await core.rpc(
                "coffee.load", _with_ids({"recipe": str(recipe)})
            )
        await _drain_release(core)
        assert core.status()["connected"] is True
        assert core.status()["connection_scope"] == "explicit"
        assert fake.calls.count("disconnect") == 0
        await core.rpc("disconnect")
        await core.shutdown()

    asyncio.run(auto_connect_cleanup())
    asyncio.run(explicit_kept())


def test_terminal_during_control_does_not_deadlock_and_releases(tmp_path):
    core, fake = _core(tmp_path)
    fake.coffee_terminal_on_pause = True
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        result = await core.rpc("pause", _with_ids(workflow_id=wid))
        assert result["terminal_during_control"] is True
        assert result["activity"] is None
        # Terminal result is visible before/without waiting on disconnect.
        assert core.status()["last_operation"]["result"] == "ready"
        await _drain_release(core)
        status = core.status()
        assert status["connected"] is False
        assert status["last_operation"]["result"] == "ready"
        assert status["last_disconnect_reason"] == "natural_terminal"
        assert status["running"] is True
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("close_session") == 1
    assert fake.calls.count("disconnect") == 1


def test_disconnect_failure_preserves_terminal_result(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )

        async def boom_close():
            fake.calls.append("close_session")
            raise RuntimeError("close_session blew up")

        async def boom_disconnect():
            fake.is_connected = False
            fake.calls.append("disconnect")
            raise RuntimeError("disconnect blew up")

        fake.close_session = boom_close  # type: ignore[method-assign]
        fake.disconnect = boom_disconnect  # type: ignore[method-assign]
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core)
        status = core.status()
        assert status["last_operation"]["result"] == "cancel_sent"
        assert status["connected"] is False
        assert status["last_disconnect_reason"] == "cancel"
        assert status["last_disconnect_error"] is not None
        assert "close_session" in status["last_disconnect_error"]
        # Must not retry physical cancel.
        assert fake.calls.count("coffee_cancel") == 1
        await core.shutdown()

    asyncio.run(go())

# ---------------------------------------------------------------------------
# Phase A focused A10 tests (durable workflow, idempotency, terminal, recovery)
# ---------------------------------------------------------------------------


def test_duplicate_load_and_start_single_ble_write(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        load_req = _rid("load")
        first = await core.rpc(
            "coffee.load",
            _with_ids({"recipe": str(recipe)}, request_id=load_req),
        )
        second = await core.rpc(
            "coffee.load",
            _with_ids({"recipe": str(recipe)}, request_id=load_req),
        )
        assert first["workflow_id"] == second["workflow_id"]
        assert fake.calls.count(("load_recipe", "Bridge test")) == 1

        wid = first["workflow_id"]
        start_req = _rid("start")
        s1 = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        s2 = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        assert s1["status"] == s2["status"] == "running"
        assert fake.calls.count("coffee_start") == 1
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_request_id_method_and_params_conflict(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    other = _recipe(tmp_path / "other.yaml")

    async def go():
        req = _rid("conflict")
        # Reserve via successful load, then cancel so a later call hits
        # idempotency identity checks rather than "already loaded".
        loaded = await core.rpc(
            "coffee.load",
            _with_ids({"recipe": str(recipe)}, request_id=req),
        )
        await core.rpc("cancel", _with_ids(workflow_id=loaded["workflow_id"]))
        await _drain_release(core)

        with pytest.raises(BridgeError, match="idempotency conflict|method mismatch"):
            await core.rpc(
                "grinder.start",
                _with_ids(
                    {
                        "size": 50,
                        "rpm": 100,
                        "seconds": 5,
                        "confirmation": GRINDER_READY_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        # Same method, different semantic params.
        with pytest.raises(BridgeError, match="params hash|idempotency conflict"):
            await core.rpc(
                "coffee.load",
                _with_ids({"recipe": str(other)}, request_id=req),
            )
        await core.shutdown()

    asyncio.run(go())


def test_wrong_workflow_id_rejected_before_ble_write(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        before = list(fake.calls)
        with pytest.raises(BridgeError, match="does not match active workflow"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id="wf_stale_not_active",
                ),
            )
        assert fake.calls == before
        assert "coffee_start" not in fake.calls
        await core.rpc(
            "cancel", _with_ids(workflow_id=loaded["workflow_id"])
        )
        await core.shutdown()

    asyncio.run(go())


def test_emergency_stop_allows_missing_workflow_id(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        result = await core.rpc(
            "cancel",
            _with_ids({"emergency": True}),  # no workflow_id
        )
        assert result["status"] == "cancel_sent"
        assert result.get("emergency") is True
        await _drain_release(core)
        events = core.store.list_workflow_events(wid)
        terminal = [e for e in events if e["event_type"] == "terminal"]
        assert terminal
        assert terminal[-1]["payload"].get("emergency") is True
        await core.shutdown()

    asyncio.run(go())
    assert "coffee_cancel" in fake.calls


def test_persistence_failure_prevents_ble_release(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )

        def boom_terminal(*_a, **_k):
            raise storage.StorageError("injected terminal commit failure")

        core.store.commit_workflow_terminal = boom_terminal  # type: ignore[method-assign]
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status = core.status()
        assert status["phase"] == "recovery_required"
        assert status["connected"] is True  # release withheld
        assert status["release_pending"] is False
        assert fake.calls.count("disconnect") == 0
        assert status["recovery"]["required"] is True
        # Disconnect failure path is separate; terminal state not rolled back.
        assert status["last_operation"]["result"] == "ready"
        assert status["last_operation"].get("persistence_failed") is True
        # Restore real commit so force-shutdown recovery can finish cleanly.
        import xbloom_storage as _storage

        core.store.commit_workflow_terminal = (  # type: ignore[method-assign]
            _storage.StateStore.commit_workflow_terminal.__get__(
                core.store, _storage.StateStore
            )
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_durable_event_cursor_no_artificial_gaps(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        page1 = await core.rpc("events", {"workflow_id": wid, "since": 0})
        assert page1["source"] == "durable"
        assert page1["gap_detected"] is False
        assert page1["events"]
        next_since = page1["next_since"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        page2 = await core.rpc(
            "events", {"workflow_id": wid, "since": next_since}
        )
        assert page2["gap_detected"] is False
        seqs = [e["seq"] for e in page1["events"] + page2["events"]]
        assert seqs == sorted(seqs)
        # Dense: no holes in durable sequence for this workflow.
        all_events = core.store.list_workflow_events(wid)
        for i, ev in enumerate(all_events, start=1):
            assert ev["seq"] == i
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())


def test_one_shot_grinder_has_durable_workflow_identity(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        started = await core.rpc(
            "grinder.start",
            _with_ids(
                {
                    "size": 50,
                    "rpm": 100,
                    "seconds": 10,
                    "confirmation": GRINDER_READY_SENTINEL,
                }
            ),
        )
        assert started["workflow_id"]
        assert started["kind"] == "grinder"
        assert started["snapshot_sha256"]
        status = core.status()
        assert status["active_workflow_id"] == started["workflow_id"]
        assert status["workflow"]["kind"] == "grinder"
        await core.rpc(
            "cancel", _with_ids(workflow_id=started["workflow_id"])
        )
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_daemon_reconstruction_from_durable_state_no_auto_connect_or_start(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        assert core.connected is True
        assert not core.coffee_state_file.exists()
        # Simulate process loss: close durable store without terminal / without
        # auto-start. BLE link is process-local and dies with the process.
        # Durable workflow alone reconstructs loaded + recovery (no coffee JSON).
        core.store.close()
        fake2 = FakeBridgeClient("AA:BB")
        # Reconcile gate requires confirmed armed after restart.
        fake2.status_state = 0x1F
        fake2.status_state_name = "armed"
        core2 = BridgeCore(
            default_address="AA:BB",
            state_dir=tmp_path,
            client_factory=lambda _a: fake2,
            environ=_environment(),
            machine_info_timeout=0.1,
        )
        assert core2.active_workflow_id == wid
        assert core2.activity == "coffee"
        assert core2.phase == "loaded"
        assert core2.connected is False
        assert core2._loaded_needs_reconcile is True
        assert not core2.coffee_state_file.exists()
        assert fake2.calls == []  # no auto-connect, no auto-start
        status = core2.status()
        assert status["active_workflow_id"] == wid
        assert status["connected"] is False
        assert status["running"] is True
        # Explicit start after reconstruct may proceed only after reconcile;
        # never auto-fired and never re-loads.
        await core2.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert fake2.calls.count("coffee_start") == 1
        assert ("load_recipe", "Bridge test") not in fake2.calls
        assert "request_status" in fake2.calls
        await core2.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core2)
        await core2.shutdown()

    asyncio.run(go())


def test_status_exposes_workflow_recovery_and_versions(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        assert core.status()["active_workflow_id"] is None
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        status = core.status()
        assert status["active_workflow_id"] == loaded["workflow_id"]
        assert status["workflow"]["workflow_id"] == loaded["workflow_id"]
        assert status["workflow"]["snapshot_sha256"]
        assert status["rpc_protocol_current"] == bridge_mod.RPC_PROTOCOL_CURRENT
        assert status["instance_id"] == core.instance_id
        assert status["core_version"]
        assert status["connection_scope"] == "workflow"
        assert "recovery" in status
        await core.rpc(
            "cancel", _with_ids(workflow_id=loaded["workflow_id"])
        )
        await core.shutdown()

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Phase A correctness pass (Codex review blocking findings)
# ---------------------------------------------------------------------------


def test_duplicate_start_after_terminal_returns_cached(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        start_req = _rid("start_term")
        first = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        fake.emit(_event(state=0x22, name="starting"))
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await _drain_release(core)
        assert core.activity is None
        before = list(fake.calls)
        second = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        assert second["status"] == first["status"] == "running"
        assert fake.calls == before
        assert fake.calls.count("coffee_start") == 1
        await core.shutdown()

    asyncio.run(go())


def test_duplicate_pause_after_phase_change_returns_cached(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        pause_req = _rid("pause_dup")
        first = await core.rpc(
            "pause", _with_ids(workflow_id=wid, request_id=pause_req)
        )
        assert first["status"] == "paused"
        assert core.phase == "paused"
        before = list(fake.calls)
        second = await core.rpc(
            "pause", _with_ids(workflow_id=wid, request_id=pause_req)
        )
        assert second == first
        assert fake.calls == before
        assert fake.calls.count("coffee_pause") == 1
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())


def test_duplicate_cancel_after_terminal_returns_cached(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        cancel_req = _rid("cancel_dup")
        first = await core.rpc(
            "cancel", _with_ids(workflow_id=wid, request_id=cancel_req)
        )
        await _drain_release(core)
        assert core.activity is None
        before = list(fake.calls)
        second = await core.rpc(
            "cancel", _with_ids(workflow_id=wid, request_id=cancel_req)
        )
        assert second["status"] == first["status"] == "cancel_sent"
        assert fake.calls == before
        assert fake.calls.count("coffee_cancel") == 1
        # Emergency stop duplicates also cache after terminal.
        emerg_req = _rid("emerg")
        loaded2 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=loaded2["workflow_id"],
            ),
        )
        e1 = await core.rpc(
            "cancel",
            _with_ids({"emergency": True}, request_id=emerg_req),
        )
        await _drain_release(core)
        e2 = await core.rpc(
            "cancel",
            _with_ids({"emergency": True}, request_id=emerg_req),
        )
        assert e1["status"] == e2["status"] == "cancel_sent"
        await core.shutdown()

    asyncio.run(go())


def test_duplicate_one_shot_start_after_terminal_and_cooldown(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        start_req = _rid("grind_dup")
        params = {
            "size": 50,
            "rpm": 100,
            "seconds": 0.2,
            "confirmation": GRINDER_READY_SENTINEL,
        }
        first = await core.rpc(
            "grinder.start", _with_ids(params, request_id=start_req)
        )
        await core.rpc(
            "cancel", _with_ids(workflow_id=first["workflow_id"])
        )
        await _drain_release(core)
        # Cooldown record present; exact duplicate still returns cache.
        before = list(fake.calls)
        second = await core.rpc(
            "grinder.start", _with_ids(params, request_id=start_req)
        )
        assert second["status"] == first["status"] == "running"
        assert second["workflow_id"] == first["workflow_id"]
        assert fake.calls == before
        assert sum(1 for c in fake.calls if c[0] == "grinder_start") == 1
        await core.shutdown()

    asyncio.run(go())


def test_coffee_load_ack_loss_pending_no_retry(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.fail_coffee_load = True
    load_req = _rid("load_ack")

    async def go():
        with pytest.raises(BridgeError, match="unconfirmed|do not retry"):
            await core.rpc(
                "coffee.load",
                _with_ids({"recipe": str(recipe)}, request_id=load_req),
            )
        status = core.status()
        assert status["connected"] is True
        assert status["phase"] == "load_unconfirmed"
        assert status["active_workflow_id"]
        assert status["recovery"]["required"] is True
        assert fake.calls.count(("load_recipe", "Bridge test")) == 1
        idem = core.store.get_idempotency(load_req)
        assert idem is not None
        assert idem["status"] == storage.IDEM_PENDING
        # Duplicate request_id must not reissue BLE.
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(
                "coffee.load",
                _with_ids({"recipe": str(recipe)}, request_id=load_req),
            )
        assert fake.calls.count(("load_recipe", "Bridge test")) == 1
        await core.rpc(
            "cancel",
            _with_ids(
                {"emergency": True},
                workflow_id=status["active_workflow_id"],
            ),
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_tea_load_ack_loss_pending_no_retry(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")
    fake.fail_tea_load = True
    load_req = _rid("tea_ack")

    async def go():
        with pytest.raises(BridgeError, match="unconfirmed|do not retry"):
            await core.rpc(
                "tea.load",
                _with_ids({"recipe": str(recipe)}, request_id=load_req),
            )
        status = core.status()
        assert status["connected"] is True
        assert status["phase"] == "load_unconfirmed"
        assert status["active_workflow_id"]
        assert fake.calls.count(("tea_load", "Bridge tea test")) == 1
        idem = core.store.get_idempotency(load_req)
        assert idem["status"] == storage.IDEM_PENDING
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(
                "tea.load",
                _with_ids({"recipe": str(recipe)}, request_id=load_req),
            )
        assert fake.calls.count(("tea_load", "Bridge tea test")) == 1
        await core.shutdown(force=True)

    asyncio.run(go())


def test_terminal_idempotency_atomic_success_and_rollback(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        cancel_req = _rid("atomic_cancel")
        result = await core.rpc(
            "cancel", _with_ids(workflow_id=wid, request_id=cancel_req)
        )
        assert result["status"] == "cancel_sent"
        await _drain_release(core)
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert wf["recovery"] is None  # CLEAR_RECOVERY on success
        idem = core.store.get_idempotency(cancel_req)
        assert idem["status"] == storage.IDEM_COMPLETED
        assert idem["result"]["status"] == "cancel_sent"
        # Exactly one final history row for cancel terminal.
        hist_key = storage.workflow_terminal_history_dedupe_key(wid)
        hist = core.store.get_history_event_by_dedupe_key(hist_key)
        assert hist is not None
        assert hist["workflow_id"] == wid
        assert hist["outcome"] == "cancelled"
        assert core.store.count_history_events() == 1

        # Rollback: inject failure mid terminal+idempotency+history transaction.
        loaded2 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid2 = loaded2["workflow_id"]
        fail_req = _rid("atomic_fail")
        real = core.store.commit_workflow_terminal

        def boom(*_a, **_k):
            raise storage.StorageError("injected atomic terminal failure")

        core.store.commit_workflow_terminal = boom  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="durable|persist|recovery"):
            await core.rpc(
                "cancel", _with_ids(workflow_id=wid2, request_id=fail_req)
            )
        core.store.commit_workflow_terminal = real  # type: ignore[method-assign]
        status = core.status()
        assert status["connected"] is True
        assert status["phase"] == "recovery_required"
        assert status["release_pending"] is False
        # BLE must still be held; cancel write already happened but durable
        # terminal+idempotency+history rolled back together.
        assert status["release_pending"] is False
        pending = core.store.get_idempotency(fail_req)
        assert pending is not None
        assert pending["status"] == storage.IDEM_PENDING
        rolled = core.store.get_workflow(wid2)
        assert rolled["terminal_at"] is None
        assert (
            core.store.get_history_event_by_dedupe_key(
                storage.workflow_terminal_history_dedupe_key(wid2)
            )
            is None
        )
        # Activity retained for recovery (terminal commit rolled back).
        assert status["connected"] is True
        await core.shutdown(force=True)

    asyncio.run(go())


def test_terminal_history_exactly_one_row_on_retry_and_natural(tmp_path):
    """Natural complete / cancel / re-commit yield exactly one history terminal row."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        # Natural completion.
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        fake.emit(_event(state=0x24, name="ready"))
        await _drain_release(core)
        key = storage.workflow_terminal_history_dedupe_key(wid)
        first = core.store.get_history_event_by_dedupe_key(key)
        assert first is not None
        assert first["outcome"] == "completed"
        assert first["workflow_id"] == wid
        assert first.get("snapshot_sha256") or first.get("recipe_name")
        # snapshot SHA must not be mislabeled as recipe_sha256 when only revision
        # content is present; path loads may still carry both.
        if first.get("recipe_sha256") and first.get("snapshot_sha256"):
            # Distinct fields when both known (path loads typically set both).
            assert isinstance(first["recipe_sha256"], str)
            assert isinstance(first["snapshot_sha256"], str)
        # Idempotent re-commit of the same terminal must not add a second history
        # row or an extra workflow_events row.
        events_before = core.store.list_workflow_events(wid)
        reentry = core.store.commit_workflow_terminal(
            wid,
            state="ready",
            event_type="terminal",
            event_payload={"result": "ready", "activity": "coffee"},
        )
        assert reentry.get("reentered") is True
        assert core.store.count_history_events() == 1
        assert len(core.store.list_workflow_events(wid)) == len(events_before)
        again = core.store.get_history_event_by_dedupe_key(key)
        assert again is not None
        assert again["event_id"] == first["event_id"]

        # Cancel path also produces exactly one history row.
        loaded2 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid2 = loaded2["workflow_id"]
        await core.rpc("cancel", _with_ids(workflow_id=wid2))
        await _drain_release(core)
        assert core.store.count_history_events() == 2
        cancel_hist = core.store.get_history_event_by_dedupe_key(
            storage.workflow_terminal_history_dedupe_key(wid2)
        )
        assert cancel_hist is not None
        assert cancel_hist["outcome"] == "cancelled"
        # History insert fault withholds release (via full terminal rollback).
        loaded3 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid3 = loaded3["workflow_id"]
        real_append = core.store._append_history_event_in_tx

        def boom_hist(*_a, **_k):
            raise storage.StorageError("injected history insert failure")

        core.store._append_history_event_in_tx = boom_hist  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="durable|persist|recovery"):
            await core.rpc("cancel", _with_ids(workflow_id=wid3))
        core.store._append_history_event_in_tx = real_append  # type: ignore[method-assign]
        status = core.status()
        assert status["connected"] is True
        assert status["release_pending"] is False
        assert status["phase"] == "recovery_required"
        assert core.store.get_workflow(wid3)["terminal_at"] is None
        assert (
            core.store.get_history_event_by_dedupe_key(
                storage.workflow_terminal_history_dedupe_key(wid3)
            )
            is None
        )
        assert core.store.count_history_events() == 2
        await core.shutdown(force=True)

    asyncio.run(go())


def test_machine_telemetry_persisted_and_terminal_ordering(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        fake.emit(
            _event(state=0x10, name="brewing", water_ml=40.0, cup_g=12.0)
        )
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await _drain_release(core)
        events = core.store.list_workflow_events(wid)
        types = [e["event_type"] for e in events]
        assert "machine" in types
        assert types[-1] == "terminal"
        machine_rows = [e for e in events if e["event_type"] == "machine"]
        assert any(
            (e["payload"].get("dispensed_water_ml") == 40.0)
            or (e["payload"].get("water_ml") == 40.0)
            for e in machine_rows
        )
        page = await core.rpc("events", {"workflow_id": wid, "since": 0})
        assert page["source"] == "durable"
        assert any(e["event_type"] == "machine" for e in page["events"])
        assert page["events"][-1]["event_type"] == "terminal"
        for i, ev in enumerate(events, start=1):
            assert ev["seq"] == i
        await core.shutdown()

    asyncio.run(go())


def test_workflow_create_and_transition_rollback(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        # Creation atomicity: event append failure rolls back the workflow row.
        real_create = core.store.create_workflow_with_event

        def boom_create(**_kwargs):
            raise storage.StorageError("injected create failure")

        core.store.create_workflow_with_event = boom_create  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="durable workflow|create"):
            await core.rpc(
                "coffee.load", _with_ids({"recipe": str(recipe)})
            )
        core.store.create_workflow_with_event = real_create  # type: ignore[method-assign]
        assert core.store.get_active_workflow() is None
        assert ("load_recipe", "Bridge test") not in fake.calls

        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]

        real_transition = core.store.transition_workflow

        def boom_transition(*_a, **_k):
            raise storage.StorageError("injected transition failure")

        core.store.transition_workflow = boom_transition  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="critical workflow transition"):
            await core.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        core.store.transition_workflow = real_transition  # type: ignore[method-assign]
        # Must not claim running success when transition failed.
        assert core.phase != "running"
        assert "coffee_start" not in fake.calls or core.phase in {
            "loaded",
            "starting",
            "control_unconfirmed",
            "recovery_required",
        }
        # start BLE write happens after starting transition -- starting is critical
        # before BLE, so coffee_start should not have run.
        assert "coffee_start" not in fake.calls
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())


def test_recipe_revision_mismatch_rejected_pre_ble(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    other = _recipe(tmp_path / "other.yaml")
    other.write_text(
        other.read_text(encoding="utf-8").replace("Bridge test", "Other recipe"),
        encoding="utf-8",
    )

    async def go():
        # Seed an unrelated revision via a successful load of `other`.
        first = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(other)})
        )
        await core.rpc("cancel", _with_ids(workflow_id=first["workflow_id"]))
        await _drain_release(core)
        bad_rev = first["recipe_revision_id"]
        before = list(fake.calls)
        with pytest.raises(
            BridgeError, match="content hash|does not match|kind"
        ):
            await core.rpc(
                "coffee.load",
                _with_ids(
                    {
                        "recipe": str(recipe),
                        "recipe_revision_id": bad_rev,
                    }
                ),
            )
        assert fake.calls == before
        assert ("load_recipe", "Bridge test") not in fake.calls
        await core.shutdown()

    asyncio.run(go())


def test_terminal_during_start_no_resurrection(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.coffee_terminal_on_start = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        start_req = _rid("start_term_race")
        result = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        assert result.get("terminal_during_start") is True
        assert result.get("status") != "running"
        assert core.phase in {"idle", "disconnected", "recovery_required"}
        assert core.activity is None
        # Must not rewrite durable state back to running after terminal.
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert wf["state"] != "running"
        assert not any(
            e["event_type"] == "started" for e in core.store.list_workflow_events(wid)
        )
        await _drain_release(core)
        # Exact duplicate returns cache; no second BLE start.
        before = list(fake.calls)
        cached = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        assert cached.get("terminal_during_start") is True
        assert cached.get("status") != "running"
        assert fake.calls == before
        await core.shutdown()

    asyncio.run(go())


def test_terminal_during_start_durable_fail_no_running_resurrection(tmp_path):
    """If terminal arrives during start but durable commit fails, never claim running."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.coffee_terminal_on_start = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]

        def boom(*_a, **_k):
            raise storage.StorageError("injected terminal during start")

        core.store.commit_workflow_terminal = boom  # type: ignore[method-assign]
        start_req = _rid("start_term_fail")
        result = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        assert result.get("terminal_during_start") is True
        assert result.get("status") != "running"
        assert result.get("recovery_required") is True
        assert core.phase == "recovery_required"
        assert core.phase != "running"
        # Must not complete as successful running.
        idem = core.store.get_idempotency(start_req)
        assert idem is not None
        assert idem["status"] == storage.IDEM_PENDING
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is None
        assert wf["state"] != "running"
        # Duplicate must not reissue start.
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )
        assert fake.calls.count("coffee_start") == 1
        core.store.commit_workflow_terminal = (  # type: ignore[method-assign]
            storage.StateStore.commit_workflow_terminal.__get__(
                core.store, storage.StateStore
            )
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_mutating_methods_cover_settings_and_exclude_connect(tmp_path):
    # Protocol honesty: every machine-mutating bridge method is listed; connect
    # / disconnect are intentionally not machine-action idempotent.
    assert "settings.write" in bridge_mod.MUTATING_METHODS
    assert "advanced.write" in bridge_mod.MUTATING_METHODS
    assert "presets.save" in bridge_mod.MUTATING_METHODS
    assert "connect" not in bridge_mod.MUTATING_METHODS
    assert "disconnect" not in bridge_mod.MUTATING_METHODS
    assert "coffee.load" in bridge_mod.MUTATING_METHODS
    assert "cancel" in bridge_mod.MUTATING_METHODS
    assert "settings.read" not in bridge_mod.MUTATING_METHODS
    assert "advanced.read" not in bridge_mod.MUTATING_METHODS


# ---------------------------------------------------------------------------
# Phase A safety/correctness blockers (Codex review correction pass)
# ---------------------------------------------------------------------------


def test_completed_duplicate_rejects_stale_explicit_workflow_id(tmp_path):
    """After terminal, same request_id + different explicit workflow_id conflicts."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        start_req = _rid("start_stale_wf")
        first = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        fake.emit(_event(state=0x22, name="starting"))
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await _drain_release(core)
        assert core.activity is None
        # Exact duplicate with original workflow_id still caches.
        cached = await core.rpc(
            "coffee.start",
            _with_ids(
                {"confirmation": READY_SENTINEL},
                workflow_id=wid,
                request_id=start_req,
            ),
        )
        assert cached["status"] == first["status"]
        # Explicit different/stale workflow_id is a conflict, not a cache hit.
        before = list(fake.calls)
        with pytest.raises(
            BridgeError, match="workflow_id mismatch|idempotency conflict"
        ):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id="wf_stale_other",
                    request_id=start_req,
                ),
            )
        assert fake.calls == before
        assert fake.calls.count("coffee_start") == 1
        await core.shutdown()

    asyncio.run(go())


def test_stop_and_cancel_preserve_rpc_method_identity(tmp_path):
    """rpc(stop) and rpc(cancel) reserve/cache under their own method names."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        cancel_req = _rid("cancel_id")
        first = await core.rpc(
            "cancel", _with_ids(workflow_id=wid, request_id=cancel_req)
        )
        await _drain_release(core)
        idem = core.store.get_idempotency(cancel_req)
        assert idem["method"] == "cancel"
        assert idem["status"] == storage.IDEM_COMPLETED
        # Same request_id as stop is a method conflict, not emergency rename.
        with pytest.raises(BridgeError, match="method mismatch|idempotency conflict"):
            await core.rpc(
                "stop",
                _with_ids(
                    {"emergency": True},
                    workflow_id=wid,
                    request_id=cancel_req,
                ),
            )
        # Fresh stop path caches under method=stop.
        loaded2 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        stop_req = _rid("stop_id")
        s1 = await core.rpc(
            "stop",
            _with_ids(workflow_id=loaded2["workflow_id"], request_id=stop_req),
        )
        await _drain_release(core)
        stop_idem = core.store.get_idempotency(stop_req)
        assert stop_idem["method"] == "stop"
        assert stop_idem["status"] == storage.IDEM_COMPLETED
        s2 = await core.rpc(
            "stop",
            _with_ids(workflow_id=loaded2["workflow_id"], request_id=stop_req),
        )
        assert s2["status"] == s1["status"]
        # Emergency cancel still stores method=cancel (not renamed to stop).
        loaded3 = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        emerg_req = _rid("emerg_cancel")
        await core.rpc(
            "cancel",
            _with_ids(
                {"emergency": True},
                workflow_id=loaded3["workflow_id"],
                request_id=emerg_req,
            ),
        )
        await _drain_release(core)
        emerg_idem = core.store.get_idempotency(emerg_req)
        assert emerg_idem["method"] == "cancel"
        await core.shutdown()

    asyncio.run(go())
    assert "coffee_cancel" in fake.calls


def test_reconstruct_created_loading_never_start(tmp_path):
    """created/loading reconstruct must not map to loaded or allow start writes."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    # Seed a durable loading workflow without completing BLE load.
    snap = {"name": "Bridge test", "kind": "coffee"}
    wf = core.store.create_workflow(
        kind="coffee",
        snapshot=snap,
        state="loading",
        source="test",
        owner="bridge",
        metadata={"recipe_path": str(recipe), "recipe_name": recipe.name},
    )
    wid = wf["workflow_id"]
    core.store.close()

    fake2 = FakeBridgeClient("AA:BB")
    core2 = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _a: fake2,
        environ=_environment(),
        machine_info_timeout=0.1,
    )
    assert core2.active_workflow_id == wid
    assert core2.phase == "loading"
    assert core2.phase != "loaded"
    assert core2._recovery_required is True
    assert fake2.calls == []

    async def go():
        before = list(fake2.calls)
        with pytest.raises(BridgeError, match="created/loading|recovery_required|do not start"):
            await core2.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert fake2.calls == before
        assert "coffee_start" not in fake2.calls
        assert ("load_recipe", "Bridge test") not in fake2.calls
        await core2.shutdown(force=True)

    asyncio.run(go())


def test_reconstruct_loaded_rejects_start_without_armed_confirm(tmp_path):
    """Reconstructed loaded must not start when machine armed cannot be confirmed."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        core.store.close()
        fake2 = FakeBridgeClient("AA:BB")
        # request_status returns info only -- not armed.
        fake2.status_state = None
        core2 = BridgeCore(
            default_address="AA:BB",
            state_dir=tmp_path,
            client_factory=lambda _a: fake2,
            environ=_environment(),
            machine_info_timeout=0.05,
        )
        assert core2.phase == "loaded"
        assert core2._loaded_needs_reconcile is True
        with pytest.raises(
            BridgeError,
            match=(
                "cannot confirm armed|no fresh state|recovery_required|do not start"
            ),
        ):
            await core2.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert "coffee_start" not in fake2.calls
        assert ("load_recipe", "Bridge test") not in fake2.calls
        # Pre-start failure: retain durable ownership; connection held for recovery.
        assert core2.active_workflow_id == wid
        assert core2._recovery_required is True
        assert core2.connected is True
        await core2.shutdown(force=True)

    asyncio.run(go())


def test_reconstruct_loaded_stale_armed_without_fresh_state_fails(tmp_path):
    """Stale machine_state=armed must not pass without a post-query state notification."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        core.store.close()
        fake2 = FakeBridgeClient("AA:BB")
        # Status query emits machine_info only (no state byte) -- proves sleep(0)
        # / stale cache is insufficient.
        fake2.status_state = None
        core2 = BridgeCore(
            default_address="AA:BB",
            state_dir=tmp_path,
            client_factory=lambda _a: fake2,
            environ=_environment(),
            machine_info_timeout=0.05,
        )
        assert core2.phase == "loaded"
        assert core2._loaded_needs_reconcile is True
        # Inject stale armed evidence before start (as if a prior session left it).
        core2.machine_state = "armed"
        with pytest.raises(
            BridgeError,
            match="no fresh state|recovery_required|do not start",
        ):
            await core2.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert "coffee_start" not in fake2.calls
        assert "request_status" in fake2.calls
        assert ("load_recipe", "Bridge test") not in fake2.calls
        assert core2.active_workflow_id == wid
        assert core2._recovery_required is True
        assert core2._loaded_needs_reconcile is True
        assert core2.connected is True
        await core2.shutdown(force=True)

    asyncio.run(go())


def test_reconstruct_loaded_fresh_coffee_armed_passes(tmp_path):
    """Fresh post-query armed state allows coffee start without re-load."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        core.store.close()
        fake2 = FakeBridgeClient("AA:BB")
        fake2.status_state = 0x1F
        fake2.status_state_name = "armed"
        core2 = BridgeCore(
            default_address="AA:BB",
            state_dir=tmp_path,
            client_factory=lambda _a: fake2,
            environ=_environment(),
            machine_info_timeout=0.05,
        )
        await core2.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert fake2.calls.count("coffee_start") == 1
        assert "request_status" in fake2.calls
        assert ("load_recipe", "Bridge test") not in fake2.calls
        assert core2._loaded_needs_reconcile is False
        await core2.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core2)
        await core2.shutdown()

    asyncio.run(go())


def test_reconstruct_loaded_tea_none_status_never_starts(tmp_path):
    """Tea reconstructed-loaded: status without state never writes start_tea."""

    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        core.store.close()
        fake2 = FakeBridgeClient("AA:BB")
        fake2.status_state = None
        core2 = BridgeCore(
            default_address="AA:BB",
            state_dir=tmp_path,
            client_factory=lambda _a: fake2,
            environ=_environment(),
            machine_info_timeout=0.05,
        )
        assert core2.phase == "loaded"
        assert core2.activity == "tea"
        with pytest.raises(
            BridgeError,
            match=(
                "no positive protocol marker|no fresh state|"
                "recovery_required|do not start"
            ),
        ):
            await core2.rpc(
                "tea.start",
                _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
            )
        assert "tea_start" not in fake2.calls
        assert ("tea_load", "Bridge tea test") not in fake2.calls
        assert core2.active_workflow_id == wid
        assert core2._recovery_required is True
        assert core2.connected is True
        await core2.rpc("cancel", _with_ids(workflow_id=wid, emergency=True))
        await _drain_release(core2)
        await core2.shutdown(force=True)

    asyncio.run(go())


def test_reconstruct_loaded_tea_idle_never_starts(tmp_path):
    """Tea reconstructed-loaded: fresh idle/ready is not a positive loaded marker."""

    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        core.store.close()
        fake2 = FakeBridgeClient("AA:BB")
        # Fresh non-active state still must not authorize tea start.
        fake2.status_state = 0x24
        fake2.status_state_name = "ready"
        core2 = BridgeCore(
            default_address="AA:BB",
            state_dir=tmp_path,
            client_factory=lambda _a: fake2,
            environ=_environment(),
            machine_info_timeout=0.05,
        )
        assert core2.phase == "loaded"
        assert core2.activity == "tea"
        with pytest.raises(
            BridgeError,
            match="no positive protocol marker|recovery_required|do not start",
        ):
            await core2.rpc(
                "tea.start",
                _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
            )
        assert "tea_start" not in fake2.calls
        assert "request_status" in fake2.calls
        assert ("tea_load", "Bridge tea test") not in fake2.calls
        assert core2.active_workflow_id == wid
        assert core2._recovery_required is True
        assert core2.connected is True
        await core2.rpc("cancel", _with_ids(workflow_id=wid, emergency=True))
        await _drain_release(core2)
        await core2.shutdown(force=True)

    asyncio.run(go())


def test_terminal_during_pause_durable_fail_no_paused_resurrection(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.coffee_terminal_on_pause = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )

        def boom(*_a, **_k):
            raise storage.StorageError("injected terminal during pause")

        core.store.commit_workflow_terminal = boom  # type: ignore[method-assign]
        pause_req = _rid("pause_term_fail")
        result = await core.rpc(
            "pause", _with_ids(workflow_id=wid, request_id=pause_req)
        )
        assert result.get("terminal_during_control") is True
        assert result.get("status") != "paused"
        assert result.get("recovery_required") is True
        assert core.phase == "recovery_required"
        assert core.phase != "paused"
        idem = core.store.get_idempotency(pause_req)
        assert idem["status"] == storage.IDEM_PENDING
        wf = core.store.get_workflow(wid)
        assert wf["state"] != "paused"
        assert wf["terminal_at"] is None
        core.store.commit_workflow_terminal = (  # type: ignore[method-assign]
            storage.StateStore.commit_workflow_terminal.__get__(
                core.store, storage.StateStore
            )
        )
        await core.shutdown(force=True)

    asyncio.run(go())


def test_terminal_during_resume_no_running_resurrection(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.coffee_terminal_on_resume = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        await core.rpc("pause", _with_ids(workflow_id=wid))
        assert core.phase == "paused"
        resume_req = _rid("resume_term")
        result = await core.rpc(
            "resume", _with_ids(workflow_id=wid, request_id=resume_req)
        )
        assert result.get("terminal_during_control") is True
        assert result.get("status") != "running"
        assert core.phase in {"idle", "disconnected", "recovery_required"}
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert wf["state"] != "running"
        assert not any(
            e["event_type"] == "resumed"
            for e in core.store.list_workflow_events(wid)
        )
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_telemetry_terminal_resolves_control_unconfirmed_coffee(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.fail_coffee_start = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        with pytest.raises(BridgeError, match="unconfirmed|do not retry"):
            await core.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert core.phase == "control_unconfirmed"
        assert core.active_workflow_id == wid
        # Later confirmed terminal must resolve uncertain control.
        fake.emit(_event(state=0x22, name="starting"))
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await _drain_release(core)
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert core.activity is None
        assert core.phase in {"idle", "disconnected"}
        assert core.connected is False
        await core.shutdown()

    asyncio.run(go())


def test_telemetry_terminal_resolves_control_unconfirmed_tea(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "tea.start",
            _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
        )
        # Force uncertain control phase after a confirmed start boundary.
        core.phase = "control_unconfirmed"
        core._saw_active = True
        core.store.update_workflow(
            wid, state="control_unconfirmed", recovery={"reason": "test"}
        )
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await _drain_release(core)
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert core.activity is None
        assert core.connected is False
        await core.shutdown()

    asyncio.run(go())


def test_scale_stop_terminal_and_idempotency_one_transaction(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        started = await core.rpc(
            "scale.start",
            _with_ids({"duration_s": 30.0, "tare": False}),
        )
        wid = started["workflow_id"]
        cancel_req = _rid("scale_cancel_ok")
        result = await core.rpc(
            "cancel", _with_ids(workflow_id=wid, request_id=cancel_req)
        )
        assert result["status"] == "stopped"
        await _drain_release(core)
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        idem = core.store.get_idempotency(cancel_req)
        assert idem["status"] == storage.IDEM_COMPLETED
        assert idem["result"]["status"] == "stopped"

        # Rollback: terminal+idempotency fail together; release withheld.
        started2 = await core.rpc(
            "scale.start",
            _with_ids({"duration_s": 30.0, "tare": False}),
        )
        wid2 = started2["workflow_id"]
        fail_req = _rid("scale_cancel_fail")
        real = core.store.commit_workflow_terminal

        def boom(*_a, **_k):
            raise storage.StorageError("injected scale terminal failure")

        core.store.commit_workflow_terminal = boom  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="durable|persist|recovery"):
            await core.rpc(
                "cancel", _with_ids(workflow_id=wid2, request_id=fail_req)
            )
        core.store.commit_workflow_terminal = real  # type: ignore[method-assign]
        status = core.status()
        assert status["connected"] is True
        assert status["phase"] == "recovery_required"
        assert status["release_pending"] is False
        pending = core.store.get_idempotency(fail_req)
        assert pending is not None
        assert pending["status"] == storage.IDEM_PENDING
        rolled = core.store.get_workflow(wid2)
        assert rolled["terminal_at"] is None
        await core.shutdown(force=True)

    asyncio.run(go())


def test_load_failed_persist_failure_retains_ownership(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        # Fail pre-BLE by making connect raise after workflow create.
        real_connect = FakeBridgeClient.connect

        async def boom_connect(self):
            self.calls.append("connect")
            raise RuntimeError("simulated connect failure")

        FakeBridgeClient.connect = boom_connect  # type: ignore[method-assign]
        real_commit = core.store.commit_workflow_terminal

        def boom_terminal(*_a, **_k):
            raise storage.StorageError("injected load_failed persist failure")

        core.store.commit_workflow_terminal = boom_terminal  # type: ignore[method-assign]
        load_req = _rid("load_dual_fail")
        with pytest.raises(BridgeError, match="recovery_required|load_failed"):
            await core.rpc(
                "coffee.load",
                _with_ids({"recipe": str(recipe)}, request_id=load_req),
            )
        FakeBridgeClient.connect = real_connect  # type: ignore[method-assign]
        core.store.commit_workflow_terminal = real_commit  # type: ignore[method-assign]
        status = core.status()
        assert status["active_workflow_id"] is not None
        assert status["phase"] == "recovery_required"
        assert status["recovery"]["required"] is True
        # Durable active row still present -- ownership retained.
        active = core.store.get_active_workflow()
        assert active is not None
        assert active["workflow_id"] == status["active_workflow_id"]
        assert active["terminal_at"] is None
        idem = core.store.get_idempotency(load_req)
        assert idem is not None
        assert idem["status"] == storage.IDEM_PENDING
        # No silent clear + pretend gone.
        assert ("load_recipe", "Bridge test") not in fake.calls
        await core.shutdown(force=True)

    asyncio.run(go())

# ---------------------------------------------------------------------------
# Phase A2/A5/A7: settings, advanced, presets, orphan idle release
# ---------------------------------------------------------------------------


def test_settings_write_duplicate_after_terminal_single_ble_write(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    req = _rid("settings_dup")

    async def go():
        first = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
                request_id=req,
            ),
        )
        await _drain_release(core)
        assert core.connected is False
        assert first["status"] == "written_and_read_back"
        before = list(fake.calls)
        second = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
                request_id=req,
            ),
        )
        assert second["status"] == first["status"]
        assert second["workflow_id"] == first["workflow_id"]
        assert fake.calls == before
        writes = [
            c for c in fake.calls if isinstance(c, tuple) and c[0] == "set_machine_settings"
        ]
        # One intentional write only (no second attempt on exact duplicate).
        assert len(writes) == 1
        await core.shutdown()

    asyncio.run(go())


def test_settings_write_params_and_method_conflict(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    req = _rid("settings_conflict")

    async def go():
        await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
                request_id=req,
            ),
        )
        await _drain_release(core)
        with pytest.raises(BridgeError, match="params hash mismatch|idempotency conflict"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "low",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        with pytest.raises(BridgeError, match="method mismatch|idempotency conflict"):
            await core.rpc(
                "advanced.write",
                _with_ids(
                    {
                        "pour_radius_level": 4,
                        "confirmation": ADVANCED_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        await core.shutdown()

    asyncio.run(go())
    writes = [
        c for c in fake.calls if isinstance(c, tuple) and c[0] == "set_machine_settings"
    ]
    assert len(writes) == 1


def test_settings_write_pending_ack_loss_no_retry_no_release(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    # First write fails; rollback also fails => unconfirmed, pending.
    fake.fail_settings_writes = 2
    req = _rid("settings_pending")

    async def go():
        with pytest.raises(BridgeError, match="rollback_confirmed=False|recovery_required"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        status = core.status()
        assert status["connected"] is True
        assert status["activity"] == "settings"
        assert status["phase"] == "control_unconfirmed"
        assert status["active_workflow_id"]
        assert status["recovery"]["required"] is True
        idem = core.store.get_idempotency(req)
        assert idem["status"] == storage.IDEM_PENDING
        write_count = sum(
            1
            for c in fake.calls
            if isinstance(c, tuple) and c[0] == "set_machine_settings"
        )
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        write_count_after = sum(
            1
            for c in fake.calls
            if isinstance(c, tuple) and c[0] == "set_machine_settings"
        )
        assert write_count_after == write_count
        assert core.connected is True
        await core.shutdown(force=True)

    asyncio.run(go())


def test_settings_write_confirmed_rollback_fails_retryable_and_releases(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    # First write fails; rollback succeeds => terminal failed, auto-release.
    fake.fail_settings_writes = 1
    req = _rid("settings_rollback_ok")

    async def go():
        with pytest.raises(BridgeError, match="rollback_confirmed=True"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        await _drain_release(core)
        status = core.status()
        assert status["connected"] is False
        assert status["activity"] is None
        idem = core.store.get_idempotency(req)
        assert idem["status"] == storage.IDEM_FAILED
        # Retry with same identity is allowed after clear failed (re-reserve).
        ok = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
                request_id=req,
            ),
        )
        assert ok["status"] == "written_and_read_back"
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_presets_save_partial_failure_keeps_recovery_and_link(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    recipes = [_recipe(tmp_path / f"recipe-{slot}.yaml") for slot in "abc"]
    fake.fail_save_slots = True
    req = _rid("presets_partial")

    async def go():
        with pytest.raises(BridgeError, match="unconfirmed|recovery_required"):
            await core.rpc(
                "presets.save",
                _with_ids(
                    {"recipes": [str(path) for path in recipes]},
                    request_id=req,
                ),
            )
        status = core.status()
        assert status["connected"] is True
        assert status["activity"] == "presets"
        assert status["phase"] == "control_unconfirmed"
        assert status["active_workflow_id"]
        assert status["recovery"]["required"] is True
        idem = core.store.get_idempotency(req)
        assert idem["status"] == storage.IDEM_PENDING
        assert sum(1 for c in fake.calls if isinstance(c, tuple) and c[0] == "save_slots") == 1
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(
                "presets.save",
                _with_ids(
                    {"recipes": [str(path) for path in recipes]},
                    request_id=req,
                ),
            )
        assert sum(1 for c in fake.calls if isinstance(c, tuple) and c[0] == "save_slots") == 1
        await core.shutdown(force=True)

    asyncio.run(go())


def test_settings_read_write_release_and_explicit_scope_retention(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)

    async def go():
        # Successful read releases one-shot auto-owned connection.
        await core.rpc("settings.read")
        assert core.connected is False
        assert core.connection_scope is None
        assert core.last_disconnect_reason == "settings_read_done"

        # Successful write releases after terminal.
        written = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        assert written["workflow_id"]
        await _drain_release(core)
        assert core.connected is False
        assert core.last_disconnect_reason == "settings_write_complete"

        # Explicit debug connection is retained across read/write.
        await core.rpc("connect", {})
        assert core.connection_scope == "explicit"
        assert core.connected is True
        await core.rpc("settings.read")
        assert core.connected is True
        assert core.connection_scope == "explicit"
        await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "medium",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        await _drain_release(core)
        assert core.connected is True
        assert core.connection_scope == "explicit"
        await core.rpc("disconnect")
        assert core.connected is False
        await core.shutdown()

    asyncio.run(go())


def test_idle_orphan_disconnect_and_disabled(tmp_path):
    core, fake = _core(tmp_path, settings_write=True, idle_disconnect_s=0.05)

    async def go():
        # Create a leftover one-shot link by completing a write then cancelling
        # the prompt release so only the idle fallback can clean it up.
        await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        core._cancel_pending_release()
        assert core.connected is True
        assert core.activity is None
        assert core.connection_scope == "one-shot"
        # Arm fallback (lifecycle helper; status must not arm/extend).
        core._arm_or_clear_idle_orphan_watch()
        deadline = core.status()["idle_orphan_deadline"]
        assert deadline is not None
        # status/events must neither create/reset/extend the timer.
        for _ in range(5):
            core.status()
            core.events_since(0)
        assert core.status()["idle_orphan_deadline"] == deadline
        await asyncio.sleep(0.12)
        # Idle task acquires op_lock then disconnects.
        deadline_wait = asyncio.get_event_loop().time() + 1.0
        while core.connected and asyncio.get_event_loop().time() < deadline_wait:
            await asyncio.sleep(0.02)
        assert core.connected is False
        assert core.last_disconnect_reason == "idle_orphan_disconnect"
        await core.shutdown()

    asyncio.run(go())

    core2, fake2 = _core(tmp_path / "idle0", settings_write=True, idle_disconnect_s=0)

    async def go_disabled():
        await core2.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        core2._cancel_pending_release()
        core2._arm_or_clear_idle_orphan_watch()
        assert core2.status()["idle_disconnect_s"] == 0.0
        assert core2.status()["idle_orphan_deadline"] is None
        await asyncio.sleep(0.12)
        assert core2.connected is True
        await core2.shutdown(force=True)

    asyncio.run(go_disabled())


def test_loaded_workflow_held_beyond_tiny_idle_timeout(tmp_path):
    core, fake = _core(tmp_path, idle_disconnect_s=0.05)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        assert core.connected is True
        assert core.phase == "loaded"
        assert core.activity == "coffee"
        # status must not arm/reset idle; loaded must never be timed out.
        for _ in range(3):
            core.status()
        await asyncio.sleep(0.15)
        assert core.connected is True
        assert core.phase == "loaded"
        assert core.active_workflow_id == loaded["workflow_id"]
        assert core.status()["idle_orphan_deadline"] is None
        await core.rpc("cancel", _with_ids(workflow_id=loaded["workflow_id"]))
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_presets_save_success_releases_and_duplicate_cached(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    recipes = [_recipe(tmp_path / f"recipe-{slot}.yaml") for slot in "abc"]
    req = _rid("presets_ok")

    async def go():
        first = await core.rpc(
            "presets.save",
            _with_ids(
                {"recipes": [str(path) for path in recipes]},
                request_id=req,
            ),
        )
        assert first["status"] == "saved"
        assert first["workflow_id"]
        await _drain_release(core)
        assert core.connected is False
        before = list(fake.calls)
        second = await core.rpc(
            "presets.save",
            _with_ids(
                {"recipes": [str(path) for path in recipes]},
                request_id=req,
            ),
        )
        assert second["status"] == "saved"
        assert second["workflow_id"] == first["workflow_id"]
        assert fake.calls == before
        assert sum(1 for c in fake.calls if isinstance(c, tuple) and c[0] == "save_slots") == 1
        await core.shutdown()

    asyncio.run(go())


@pytest.mark.parametrize(
    "method,params_builder,write_call",
    [
        (
            "settings.write",
            lambda _tmp: {
                "display": "high",
                "confirmation": SETTINGS_CONFIRM_SENTINEL,
            },
            "set_machine_settings",
        ),
        (
            "advanced.write",
            lambda _tmp: {
                "pour_radius_level": 4,
                "confirmation": ADVANCED_CONFIRM_SENTINEL,
            },
            "write_advanced_settings",
        ),
        (
            "presets.save",
            lambda tmp: {
                "recipes": [
                    str(_recipe(tmp / f"slot-{slot}.yaml")) for slot in "abc"
                ]
            },
            "save_slots",
        ),
    ],
)
def test_one_shot_write_durable_terminal_fail_no_false_success(
    tmp_path, method, params_builder, write_call
):
    """Machine write may succeed, but durable terminal rollback must never claim success."""

    core, fake = _core(tmp_path, settings_write=True)
    req = _rid(f"{method}_term_fail")
    params = params_builder(tmp_path)

    async def go():
        real = core.store.commit_workflow_terminal

        def boom(*_a, **_k):
            raise storage.StorageError("injected one-shot terminal failure")

        core.store.commit_workflow_terminal = boom  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="durable|recovery_required|terminal"):
            await core.rpc(method, _with_ids(params, request_id=req))
        core.store.commit_workflow_terminal = real  # type: ignore[method-assign]

        status = core.status()
        assert status["connected"] is True
        assert status["phase"] == "recovery_required"
        assert status["activity"] in {"settings", "advanced", "presets"}
        assert status["active_workflow_id"]
        assert status["release_pending"] is False
        assert status["recovery"]["required"] is True

        active = core.store.get_active_workflow()
        assert active is not None
        assert active["workflow_id"] == status["active_workflow_id"]
        assert active["terminal_at"] is None

        idem = core.store.get_idempotency(req)
        assert idem is not None
        assert idem["status"] == storage.IDEM_PENDING

        # No success cache: exact duplicate remains blocked as pending.
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(method, _with_ids(params, request_id=req))
        write_count = sum(
            1
            for c in fake.calls
            if isinstance(c, tuple) and c[0] == write_call
        )
        assert write_count >= 1
        # No second machine write on the blocked duplicate.
        write_count_after = sum(
            1
            for c in fake.calls
            if isinstance(c, tuple) and c[0] == write_call
        )
        assert write_count_after == write_count
        assert core.connected is True
        await core.shutdown(force=True)

    asyncio.run(go())


def test_settings_write_connect_failure_marks_failed_and_is_retryable(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    req = _rid("settings_connect_fail")
    real_connect = FakeBridgeClient.connect

    async def boom_connect(self):
        self.calls.append("connect")
        raise RuntimeError("simulated settings connect failure")

    FakeBridgeClient.connect = boom_connect  # type: ignore[method-assign]

    async def go():
        with pytest.raises(Exception, match="connect failure"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        FakeBridgeClient.connect = real_connect  # type: ignore[method-assign]
        idem = core.store.get_idempotency(req)
        assert idem is not None
        assert idem["status"] == storage.IDEM_FAILED
        assert core.connected is False
        assert core.activity is None
        assert core.store.get_active_workflow() is None
        # Same request_id may retry: no machine write occurred.
        ok = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
                request_id=req,
            ),
        )
        assert ok["status"] == "written_and_read_back"
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_settings_write_workflow_create_failure_marks_failed_and_releases_orphan(
    tmp_path,
):
    core, fake = _core(tmp_path, settings_write=True)
    req = _rid("settings_wf_create_fail")

    async def go():
        # Pre-existing auto-owned orphan (e.g. leftover one-shot after cancelled release).
        await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "medium",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        core._cancel_pending_release()
        assert core.connected is True
        assert core.connection_scope == "one-shot"
        assert core.activity is None

        real_create = core.store.create_workflow_with_event

        def boom_create(*_a, **_k):
            raise storage.StorageError("injected workflow create failure")

        core.store.create_workflow_with_event = boom_create  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="failed to create durable workflow"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        core.store.create_workflow_with_event = real_create  # type: ignore[method-assign]

        idem = core.store.get_idempotency(req)
        assert idem is not None
        assert idem["status"] == storage.IDEM_FAILED
        # Pre-existing auto-owned orphan must be prompt-released (not wedged).
        await _drain_release(core)
        assert core.connected is False
        assert core.connection_scope is None
        assert core.last_disconnect_reason == "settings_write_preflight_failed"
        # Retry same request_id after clear failed (no machine write on the fail).
        ok = await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
                request_id=req,
            ),
        )
        assert ok["status"] == "written_and_read_back"
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_settings_write_preflight_retains_explicit_debug_connection(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    req = _rid("settings_preflight_explicit")

    async def go():
        await core.rpc("connect", {})
        assert core.connection_scope == "explicit"
        real_create = core.store.create_workflow_with_event

        def boom_create(*_a, **_k):
            raise storage.StorageError("injected create fail on explicit")

        core.store.create_workflow_with_event = boom_create  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="failed to create durable workflow"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=req,
                ),
            )
        core.store.create_workflow_with_event = real_create  # type: ignore[method-assign]
        assert core.store.get_idempotency(req)["status"] == storage.IDEM_FAILED
        assert core.connected is True
        assert core.connection_scope == "explicit"
        await core.rpc("disconnect")
        await core.shutdown()

    asyncio.run(go())


def test_settings_control_unconfirmed_recovery_release_truthful(tmp_path):
    core, fake = _core(tmp_path, settings_write=True)
    write_req = _rid("settings_unconf_write")
    cancel_req = _rid("settings_unconf_cancel")
    fake.fail_settings_writes = 2  # write + rollback both fail

    async def go():
        with pytest.raises(
            BridgeError, match="rollback_confirmed=False|recovery_required"
        ):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=write_req,
                ),
            )
        status = core.status()
        assert status["phase"] == "control_unconfirmed"
        assert status["activity"] == "settings"
        wid = status["active_workflow_id"]
        assert wid
        assert core.store.get_idempotency(write_req)["status"] == storage.IDEM_PENDING

        released = await core.rpc(
            "cancel", _with_ids(workflow_id=wid, request_id=cancel_req)
        )
        assert released["status"] == "recovery_released"
        assert released["result"] == "ownership_released_unconfirmed"
        assert released["machine_cancel"] is False
        assert released["machine_effect_unknown"] is True
        assert released.get("status") != "cancel_sent"
        assert released.get("result") != "cancel_sent"
        assert released.get("result") != "rollback"

        await _drain_release(core)
        status2 = core.status()
        assert status2["activity"] is None
        assert status2["connected"] is False
        assert status2["last_disconnect_reason"] == "recovery_released"
        assert status2["last_operation"]["result"] == "ownership_released_unconfirmed"
        assert status2["last_operation"].get("machine_cancel") is False

        wf = core.store.get_workflow(wid)
        assert wf is not None
        assert wf["terminal_at"] is not None
        assert wf["state"] == "ownership_released_unconfirmed"
        # Durable terminal event must not claim machine cancel.
        events = core.store.list_workflow_events(wid)
        terminal_events = [e for e in events if e.get("event_type") == "terminal"]
        assert terminal_events
        payload = terminal_events[-1].get("payload") or {}
        assert payload.get("result") == "ownership_released_unconfirmed"
        assert payload.get("machine_cancel") is False

        cancel_idem = core.store.get_idempotency(cancel_req)
        assert cancel_idem["status"] == storage.IDEM_COMPLETED
        assert cancel_idem["result"]["status"] == "recovery_released"

        # Original write stays pending forever; duplicate still blocked.
        write_idem = core.store.get_idempotency(write_req)
        assert write_idem["status"] == storage.IDEM_PENDING
        before_writes = sum(
            1
            for c in fake.calls
            if isinstance(c, tuple) and c[0] == "set_machine_settings"
        )
        with pytest.raises(BridgeError, match="recovery_required|pending"):
            await core.rpc(
                "settings.write",
                _with_ids(
                    {
                        "display": "high",
                        "confirmation": SETTINGS_CONFIRM_SENTINEL,
                    },
                    request_id=write_req,
                ),
            )
        after_writes = sum(
            1
            for c in fake.calls
            if isinstance(c, tuple) and c[0] == "set_machine_settings"
        )
        assert after_writes == before_writes
        await core.shutdown()

    asyncio.run(go())


def test_explicit_connect_upgrades_orphan_clears_idle_timeout(tmp_path):
    core, fake = _core(tmp_path, settings_write=True, idle_disconnect_s=30.0)

    async def go():
        await core.rpc(
            "settings.write",
            _with_ids(
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                }
            ),
        )
        core._cancel_pending_release()
        assert core.connected is True
        assert core.connection_scope == "one-shot"
        core._arm_or_clear_idle_orphan_watch()
        status = core.status()
        assert status["idle_orphan_since"] is not None
        assert status["idle_orphan_deadline"] is not None
        assert core._idle_orphan_task is not None

        await core.rpc("connect", {})
        assert core.connection_scope == "explicit"
        status2 = core.status()
        assert status2["idle_orphan_since"] is None
        assert status2["idle_orphan_deadline"] is None
        assert core._idle_orphan_task is None
        await core.rpc("disconnect")
        await core.shutdown()

    asyncio.run(go())


# ── Phase A6: unexpected BLE drop + explicit recovery.reconcile ─────────────


def test_idle_unexpected_drop_settles_without_recovery(tmp_path):
    """No activity/workflow: unexpected drop disconnects without inventing recovery."""

    core, fake = _core(tmp_path)

    async def go():
        await core.rpc("connect", {})
        assert core.connected is True
        assert core.activity is None
        assert core.active_workflow_id is None
        connects_before = fake.connect_count
        fake.drop_link()
        status = core.status()
        assert status["connected"] is False
        assert status["connection_scope"] is None
        assert status["phase"] == "disconnected"
        assert status["last_disconnect_reason"] == "ble_disconnected"
        assert status["recovery"] is None or status["recovery"].get("required") is not True
        assert core._recovery_required is False
        # No auto-reconnect.
        assert fake.connect_count == connects_before
        assert "connect" not in fake.calls[fake.calls.index("drop_link") + 1 :]
        await core.shutdown()

    asyncio.run(go())


def test_loaded_coffee_drop_no_auto_reconnect_start_or_load(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        assert core.phase == "loaded"
        connects_before = fake.connect_count
        load_calls_before = sum(
            1 for c in fake.calls if isinstance(c, tuple) and c[0] == "load_recipe"
        )
        fake.drop_link()
        status = core.status()
        assert status["connected"] is False
        assert status["activity"] == "coffee"
        assert status["active_workflow_id"] == wid
        assert status["phase"] == "loaded"
        assert status["connection_scope"] is None
        assert core._recovery_required is True
        assert core._loaded_needs_reconcile is True
        assert status["recovery"]["required"] is True
        assert status["last_disconnect_reason"] == "ble_disconnected"
        # Address preserved for explicit recovery.
        assert core.address == "AA:BB"
        # No auto reconnect / start / load.
        assert fake.connect_count == connects_before
        assert "coffee_start" not in fake.calls
        load_calls_after = sum(
            1 for c in fake.calls if isinstance(c, tuple) and c[0] == "load_recipe"
        )
        assert load_calls_after == load_calls_before
        events = core.store.list_workflow_events(wid)
        assert any(e.get("event_type") == "ble_disconnected" for e in events)
        await core.shutdown(force=True)

    asyncio.run(go())


def test_recovery_reconcile_fresh_armed_without_reload(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        fake.drop_link()
        assert core._recovery_required is True
        load_before = sum(
            1 for c in fake.calls if isinstance(c, tuple) and c[0] == "load_recipe"
        )
        fake.status_state = 0x1F
        fake.status_state_name = "armed"
        result = await core.rpc(
            "recovery.reconcile", {"workflow_id": wid}
        )
        assert result.get("reconciled") is True
        assert result.get("reconcile_outcome") == "loaded_armed"
        assert core._recovery_required is False
        assert core._loaded_needs_reconcile is False
        assert core.phase == "loaded"
        assert core.connected is True
        load_after = sum(
            1 for c in fake.calls if isinstance(c, tuple) and c[0] == "load_recipe"
        )
        assert load_after == load_before
        assert "coffee_start" not in fake.calls
        # Explicit start still works without re-load.
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert fake.calls.count("coffee_start") == 1
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core)
        await core.shutdown()

    asyncio.run(go())


def test_loaded_tea_drop_and_reconcile_fail_closed(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        fake.drop_link()
        assert core._recovery_required is True
        assert core.activity == "tea"
        assert core.phase == "loaded"
        fake.status_state = 0x24
        fake.status_state_name = "ready"
        with pytest.raises(
            BridgeError,
            match="no positive protocol marker|recovery_required|do not start",
        ):
            await core.rpc("recovery.reconcile", {"workflow_id": wid})
        assert "tea_start" not in fake.calls
        assert not any(
            isinstance(c, tuple) and c[0] == "tea_load" and fake.calls.index(c) > fake.calls.index("drop_link")
            for c in fake.calls
        )
        assert core._recovery_required is True
        assert core.active_workflow_id == wid
        assert core.connected is True  # link kept after successful reconnect+query
        await core.rpc("cancel", _with_ids(workflow_id=wid, emergency=True))
        await _drain_release(core)
        await core.shutdown(force=True)

    asyncio.run(go())


def test_running_drop_no_repeated_start(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert core.phase == "running"
        starts_before = fake.calls.count("coffee_start")
        fake.drop_link()
        assert core._recovery_required is True
        assert core.phase == "running"
        assert core.activity == "coffee"
        assert core.active_workflow_id == wid
        assert fake.calls.count("coffee_start") == starts_before
        # Reconcile to fresh brewing does not reissue start or load.
        drop_idx = fake.calls.index("drop_link")
        fake.status_state = 0x10
        fake.status_state_name = "brewing"
        result = await core.rpc("recovery.reconcile", {"workflow_id": wid})
        assert result.get("reconciled") is True
        assert result.get("reconcile_outcome") == "running"
        assert fake.calls.count("coffee_start") == starts_before
        assert not any(
            isinstance(c, tuple) and c[0] == "load_recipe"
            for c in fake.calls[drop_idx + 1 :]
        )
        await core.rpc("cancel", _with_ids(workflow_id=wid, emergency=True))
        await _drain_release(core)
        await core.shutdown(force=True)

    asyncio.run(go())


def test_start_in_flight_drop_leaves_pending_never_retryable(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        start_req = _rid("start_drop")

        original_start = fake.start

        async def start_then_drop():
            fake.drop_link()
            raise RuntimeError("link lost during start write")

        fake.start = start_then_drop  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="unconfirmed|recovery_required|do not retry"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )
        fake.start = original_start  # type: ignore[method-assign]
        idem = core.store.get_idempotency(start_req)
        assert idem["status"] == storage.IDEM_PENDING
        assert core._recovery_required is True
        starts = sum(1 for c in fake.calls if c == "coffee_start")
        with pytest.raises(BridgeError, match="recovery_required|pending|do not retry"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )
        # Pending is never reissued.
        assert sum(1 for c in fake.calls if c == "coffee_start") == starts
        await core.shutdown(force=True)

    asyncio.run(go())


def test_inflight_start_drop_returns_bridge_error_not_cancelled(tmp_path):
    """In-flight drop must wake start with domain error → BridgeError RPC path.

    Production client uses set_exception(XBloomError); FakeBridgeClient mirrors
    that so CancelledError never escapes to the server task.
    """

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.block_start_until_drop = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        start_req = _rid("start_block_drop")

        async def drop_after_start_blocks():
            # Wait until start has registered its waiter.
            for _ in range(50):
                if fake._op_waiters:
                    break
                await asyncio.sleep(0.01)
            assert fake._op_waiters, "start never blocked on op waiter"
            fake.drop_link()

        dropper = asyncio.create_task(drop_after_start_blocks())
        with pytest.raises(BridgeError, match="unconfirmed|do not retry|link lost|BLE") as ei:
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )
        await dropper
        # Normal BridgeError path — not asyncio.CancelledError.
        assert isinstance(ei.value, BridgeError)
        assert not isinstance(ei.value, asyncio.CancelledError)
        idem = core.store.get_idempotency(start_req)
        assert idem["status"] == storage.IDEM_PENDING
        assert core._recovery_required is True
        assert core.active_workflow_id == wid
        # Pending never reissued.
        with pytest.raises(BridgeError, match="recovery_required|pending|do not retry"):
            await core.rpc(
                "coffee.start",
                _with_ids(
                    {"confirmation": READY_SENTINEL},
                    workflow_id=wid,
                    request_id=start_req,
                ),
            )
        assert fake.calls.count("coffee_start") == 1
        await core.shutdown(force=True)

    asyncio.run(go())


def test_unconfirmed_control_then_fresh_terminal_via_reconcile_releases(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")
    fake.fail_coffee_start = True

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        with pytest.raises(BridgeError, match="unconfirmed|do not retry"):
            await core.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert core.phase == "control_unconfirmed"
        fake.drop_link()
        assert core._recovery_required is True
        # Fresh terminal via reconcile: terminalize then release.
        fake.status_state = 0x24
        fake.status_state_name = "ready"
        result = await core.rpc("recovery.reconcile", {"workflow_id": wid})
        assert result.get("reconcile_outcome") == "terminal"
        await _drain_release(core)
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert core.activity is None
        assert core.connected is False
        assert core.phase in {"idle", "disconnected"}
        # No auto-reconnect after release.
        connects = fake.connect_count
        await asyncio.sleep(0.05)
        assert fake.connect_count == connects
        await core.shutdown()

    asyncio.run(go())


def test_expected_terminal_and_explicit_disconnect_no_recovery_race(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        # Natural terminal schedules expected release.
        fake.emit(_event(state=0x24, name="ready"))
        await asyncio.sleep(0)
        await _drain_release(core)
        assert core.activity is None
        assert core.connected is False
        # Late unexpected-style callback after unbind must not invent recovery.
        fake.drop_link()
        assert core._recovery_required is False
        assert core.phase in {"idle", "disconnected"}
        wf = core.store.get_workflow(wid)
        assert wf["terminal_at"] is not None
        assert wf["state"] != "recovery_required"

        # Explicit connect/disconnect: expected path, no recovery.
        await core.rpc("connect", {})
        await core.rpc("disconnect")
        assert core._recovery_required is False
        assert core.last_disconnect_reason == "explicit"
        await core.shutdown()

    asyncio.run(go())


def test_external_busy_one_attempt_no_retry(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        fake.drop_link()
        fake.fail_connect = RuntimeError(
            "BleakDeviceBusyError: device already connected / GATT busy"
        )
        before = fake.connect_count
        with pytest.raises(BridgeError, match="device_busy_external") as ei:
            await core.rpc("recovery.reconcile", {"workflow_id": wid})
        assert getattr(ei.value, "category", None) == "device_busy_external"
        # Exactly one connect attempt for this operation.
        assert fake.connect_count == before + 1
        assert core._recovery_required is True
        assert core.active_workflow_id == wid
        # status/events remain read-only and do not connect.
        before2 = fake.connect_count
        core.status()
        core.rpc  # noqa: B018 — attribute access only
        status = await core.rpc("status")
        assert status["connected"] is False
        events = await core.rpc("events", {"since": 0})
        assert "events" in events or "next_since" in events or isinstance(events, dict)
        assert fake.connect_count == before2
        # Clearing fail still does not auto-retry in background.
        await asyncio.sleep(0.05)
        assert fake.connect_count == before2
        fake.fail_connect = None
        await core.shutdown(force=True)

    asyncio.run(go())


def test_ble_drop_persist_failure_fail_closed(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]

        def boom(*_a, **_k):
            raise storage.StorageError("injected ble_disconnected persist fail")

        core.store.transition_workflow = boom  # type: ignore[method-assign]
        fake.drop_link()
        # Fail closed in memory even if durable write fails.
        assert core._recovery_required is True
        assert core.activity == "coffee"
        assert core.active_workflow_id == wid
        assert core.connected is False
        assert "persist" in (core.last_error or "").lower() or core._recovery_detail
        assert core._recovery_detail is not None
        await core.shutdown(force=True)

    asyncio.run(go())


def test_recovery_reconcile_persist_failure_fail_closed(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        fake.drop_link()
        fake.status_state = 0x1F
        fake.status_state_name = "armed"

        real_transition = core.store.transition_workflow

        def boom(workflow_id, **kwargs):
            if kwargs.get("event_type") == "recovery_reconciled":
                raise storage.StorageError("injected reconcile persist fail")
            return real_transition(workflow_id, **kwargs)

        core.store.transition_workflow = boom  # type: ignore[method-assign]
        with pytest.raises(BridgeError, match="durable reconcile failed|recovery_required"):
            await core.rpc("recovery.reconcile", {"workflow_id": wid})
        assert core._recovery_required is True
        assert core._loaded_needs_reconcile is True
        assert core.active_workflow_id == wid
        await core.shutdown(force=True)

    asyncio.run(go())


def test_recovery_reconcile_requires_matching_workflow_id(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        fake.drop_link()
        with pytest.raises(BridgeError, match="workflow_id|does not match"):
            await core.rpc("recovery.reconcile", {"workflow_id": "wf_other"})
        with pytest.raises(BridgeError, match="workflow_id is required"):
            await core.rpc("recovery.reconcile", {})
        # Wrong id must not connect.
        assert core.connected is False
        await core.shutdown(force=True)

    asyncio.run(go())

def test_source_deleted_after_load_same_process_start_reuses_link(tmp_path):
    """Source YAML deleted after load; same-process start uses durable snapshot only."""

    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        sha = loaded["snapshot_sha256"]
        assert not core.coffee_state_file.exists()
        recipe.unlink()
        assert not recipe.exists()
        started = await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert started["status"] == "running"
        assert started["workflow_id"] == wid
        assert core.store.get_workflow(wid)["snapshot_sha256"] == sha
        assert fake.calls.count("connect") == 1
        assert fake.calls.count(("load_recipe", "Bridge test")) == 1
        assert fake.calls.count("coffee_start") == 1
        assert not core.coffee_state_file.exists()
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())


def test_source_modified_after_load_does_not_alter_durable_snapshot(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        before = core.store.get_workflow(wid)
        snap_before = dict(before["snapshot"])
        sha_before = before["snapshot_sha256"]
        recipe.write_text(
            """name: Mutated after load
dose_g: 20
grind: 40
pours:
  - {ml: 50, temp_c: 90, pattern: center, pause_s: 10, rpm: 100, flow_ml_s: 3.0}
  - {ml: 100, temp_c: 90, pattern: center, pause_s: 5, rpm: 100, flow_ml_s: 3.0}
  - {ml: 100, temp_c: 90, pattern: center, pause_s: 5, rpm: 100, flow_ml_s: 3.0}
""",
            encoding="utf-8",
        )
        started = await core.rpc(
            "coffee.start",
            _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
        )
        assert started["status"] == "running"
        after = core.store.get_workflow(wid)
        assert after["snapshot"] == snap_before
        assert after["snapshot_sha256"] == sha_before
        assert after["snapshot"].get("name") == "Bridge test"
        assert not core.coffee_state_file.exists()
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())


def test_no_coffee_tea_json_created_across_load_start_unconfirmed_terminal(tmp_path):
    core, fake = _core(tmp_path)
    fake.fail_coffee_start = True
    recipe = _recipe(tmp_path / "recipe.yaml")
    tea = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "coffee.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        assert not core.coffee_state_file.exists()
        assert not core.tea_state_file.exists()
        with pytest.raises(BridgeError, match="do not retry start"):
            await core.rpc(
                "coffee.start",
                _with_ids({"confirmation": READY_SENTINEL}, workflow_id=wid),
            )
        assert not core.coffee_state_file.exists()
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await _drain_release(core)
        assert not core.coffee_state_file.exists()

        loaded_tea = await core.rpc(
            "tea.load", _with_ids({"recipe": str(tea)})
        )
        twid = loaded_tea["workflow_id"]
        assert not core.tea_state_file.exists()
        await core.rpc(
            "tea.start",
            _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=twid),
        )
        assert not core.tea_state_file.exists()
        fake.emit(_event(state=0x01, name="idle"))
        await _drain_release(core)
        assert not core.tea_state_file.exists()
        await core.shutdown(force=True)

    asyncio.run(go())


def test_explicit_migration_imports_coffee_tea_json_byte_identical(tmp_path):
    """migrate_legacy_state imports recovery JSON without modifying originals.

    Lossless / public StateStore import verification only. Runtime no-auto-
    connect and cancel-with-imported-address behavior is covered by the
    single-record parametrized migration recovery test.
    """

    armed = {
        "address": "AA:BB:CC:DD:EE:FF",
        "status": "armed",
        "recipe_sha256": "deadbeef",
        "owner": "legacy",
    }
    tea = {"address": "AA:BB", "status": "tea_loaded", "recipe_path": "/tmp/x.yaml"}
    coffee_path = tmp_path / "armed-state.json"
    tea_path = tmp_path / "tea-loaded-state.json"
    coffee_path.write_text(json.dumps(armed, indent=2), encoding="utf-8")
    tea_path.write_text(json.dumps(tea, indent=2), encoding="utf-8")
    coffee_bytes = coffee_path.read_bytes()
    tea_bytes = tea_path.read_bytes()

    result = storage.migrate_legacy_state(tmp_path)
    assert result["status"] == "completed"
    assert coffee_path.read_bytes() == coffee_bytes
    assert tea_path.read_bytes() == tea_bytes

    coffee_sha = storage.sha256_bytes(coffee_bytes)
    tea_sha = storage.sha256_bytes(tea_bytes)
    coffee_wid = f"legacy_recovery_armed_{coffee_sha[:16]}"
    tea_wid = f"legacy_recovery_tea_{tea_sha[:16]}"

    store = storage.StateStore(tmp_path)
    try:
        assert store.count_legacy_imports("recovery_armed") == 1
        assert store.count_legacy_imports("recovery_tea") == 1
        coffee_wf = store.get_workflow(coffee_wid)
        tea_wf = store.get_workflow(tea_wid)
        assert coffee_wf is not None
        assert tea_wf is not None
        assert coffee_wf["kind"] == "coffee_recovery"
        assert tea_wf["kind"] == "tea_recovery"
        assert coffee_wf["state"] == "recovery_imported"
        assert tea_wf["state"] == "recovery_imported"
        assert coffee_wf["source"] == "legacy_migration"
        assert tea_wf["source"] == "legacy_migration"
        assert coffee_wf["recovery"] == armed
        assert tea_wf["recovery"] == tea
        assert coffee_wf["snapshot"] is None
        assert tea_wf["snapshot"] is None
        assert (coffee_wf.get("metadata") or {}).get("legacy_sha256") == coffee_sha
        assert (tea_wf.get("metadata") or {}).get("legacy_sha256") == tea_sha
    finally:
        store.close()


@pytest.mark.parametrize(
    ("source_kind", "filename", "payload", "expected_activity", "expected_call"),
    [
        (
            "recovery_armed",
            "armed-state.json",
            {
                "address": "AA:BB:CC:DD:EE:FF",
                "status": "armed",
                "recipe_sha256": "deadbeef",
                "owner": "legacy",
            },
            "coffee",
            "coffee_cancel",
        ),
        (
            "recovery_tea",
            "tea-loaded-state.json",
            {
                "address": "11:22:33:44:55:66",
                "status": "tea_loaded",
                "recipe_path": "/tmp/x.yaml",
            },
            "tea",
            "tea_unload",
        ),
    ],
)
def test_migrated_single_recovery_no_autoconnect_cancel_uses_imported_address(
    tmp_path,
    source_kind,
    filename,
    payload,
    expected_activity,
    expected_call,
):
    """One legacy recovery JSON: migrate, no auto-BLE, cancel via imported address."""

    path = tmp_path / filename
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    original_bytes = path.read_bytes()
    imported_address = payload["address"]
    file_sha = storage.sha256_bytes(original_bytes)
    expected_wid = f"legacy_{source_kind}_{file_sha[:16]}"

    result = storage.migrate_legacy_state(tmp_path)
    assert result["status"] == "completed"
    assert path.read_bytes() == original_bytes

    clients: list[FakeBridgeClient] = []
    factory_addresses: list[str] = []

    def capturing_factory(address: str) -> FakeBridgeClient:
        factory_addresses.append(address)
        client = FakeBridgeClient(address)
        clients.append(client)
        return client

    async def boom_scan(*, timeout: float = 8.0):
        raise AssertionError(f"scan must not run; timeout={timeout}")

    core = BridgeCore(
        default_address=None,
        state_dir=tmp_path,
        client_factory=capturing_factory,
        scan_fn=boom_scan,
        environ=_environment(),
        machine_info_timeout=0.1,
    )
    try:
        # Startup hydrates durable recovery only — zero BLE.
        assert factory_addresses == []
        assert clients == []
        status = core.status()
        assert status["connected"] is False
        assert status["idle"] is False
        assert status["active_workflow_id"] == expected_wid
        assert status["activity"] == expected_activity
        recovery = status.get("recovery") or {}
        assert recovery.get("required") is True
        assert (recovery.get("detail") or {}).get("workflow_id") == expected_wid
        assert core.address == imported_address
        assert core.default_address == imported_address
        assert path.read_bytes() == original_bytes

        async def go():
            cancelled = await core.rpc(
                "cancel", _with_ids(workflow_id=expected_wid)
            )
            assert cancelled["status"] == "cancel_sent"
            assert cancelled["activity"] == expected_activity
            assert cancelled["workflow_id"] == expected_wid
            await _drain_release(core)
            assert core.connected is False
            assert core.activity is None
            assert core.active_workflow_id is None
            assert core.is_idle() is True

        asyncio.run(go())

        assert factory_addresses == [imported_address]
        assert len(clients) == 1
        fake = clients[0]
        assert fake.address == imported_address
        assert fake.calls.count("connect") == 1
        assert expected_call in fake.calls
        assert fake.calls.count("disconnect") == 1

        terminal = core.store.get_workflow(expected_wid)
        assert terminal is not None
        assert terminal.get("terminal_at") is not None
        assert path.read_bytes() == original_bytes
    finally:
        asyncio.run(core.shutdown(force=True))


def test_source_deleted_after_tea_load_same_process_start_reuses_snapshot(tmp_path):
    """Source YAML deleted after tea.load; same-process start uses durable snapshot only."""

    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        sha = loaded["snapshot_sha256"]
        assert not core.tea_state_file.exists()
        recipe.unlink()
        assert not recipe.exists()
        started = await core.rpc(
            "tea.start",
            _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
        )
        assert started["status"] == "running"
        assert started["workflow_id"] == wid
        assert core.store.get_workflow(wid)["snapshot_sha256"] == sha
        assert fake.calls.count("connect") == 1
        assert not core.tea_state_file.exists()
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())


def test_source_modified_after_tea_load_does_not_alter_durable_snapshot(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc(
            "tea.load", _with_ids({"recipe": str(recipe)})
        )
        wid = loaded["workflow_id"]
        before = core.store.get_workflow(wid)
        snap_before = dict(before["snapshot"])
        sha_before = before["snapshot_sha256"]
        recipe.write_text(
            """name: Mutated tea after load
kind: tea
leaf_g: 9
output_ml_per_steep: 100
pours:
  - {ml: 90, temp_c: 95, pattern: circular, pause_s: 60, flow_ml_s: 3.5}
""",
            encoding="utf-8",
        )
        started = await core.rpc(
            "tea.start",
            _with_ids({"confirmation": TEA_READY_SENTINEL}, workflow_id=wid),
        )
        assert started["status"] == "running"
        after = core.store.get_workflow(wid)
        assert after["snapshot"] == snap_before
        assert after["snapshot_sha256"] == sha_before
        assert not core.tea_state_file.exists()
        await core.rpc("cancel", _with_ids(workflow_id=wid))
        await core.shutdown()

    asyncio.run(go())

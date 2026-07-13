"""Headless tests for the persistent bridge state machine and local transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

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
from xbloom_ble.telemetry import StatusEvent


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
        self.calls = []
        self.fail_grinder_pause = False
        self.coffee_terminal_on_pause = False
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

    def emit(self, event: StatusEvent):
        for listener in tuple(self.listeners):
            listener(event)

    async def connect(self):
        self.is_connected = True
        self.calls.append("connect")

    async def disconnect(self):
        self.is_connected = False
        self.calls.append("disconnect")

    async def open_session(self):
        self.calls.append("open_session")

    async def close_session(self):
        self.calls.append("close_session")

    async def request_status(self):
        self.calls.append("request_status")
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
        self.machine_info.update(requested)
        return dict(self.machine_info)

    async def read_advanced_settings(self):
        self.calls.append("read_advanced_settings")
        return dict(self.advanced)

    async def write_advanced_settings(self, **requested):
        self.calls.append(("write_advanced_settings", dict(requested)))
        self.advanced.update(
            {key: value for key, value in requested.items() if value is not None}
        )
        return dict(self.advanced)

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

    async def pause_coffee(self):
        self.calls.append("coffee_pause")
        if self.coffee_terminal_on_pause:
            self.emit(_event(state=0x24, name="ready"))
        return _event(command=40518)

    async def resume_coffee(self):
        self.calls.append("coffee_resume")
        return _event(command=40524)

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


def _environment(
    *, live_adjust: bool = False, settings_write: bool = False
) -> dict[str, str]:
    values = {
        REMOTE_START_ENV: REMOTE_START_SENTINEL,
        REMOTE_GRINDER_ENV: REMOTE_GRINDER_SENTINEL,
    }
    if live_adjust:
        values[LIVE_ADJUST_ENV] = LIVE_ADJUST_SENTINEL
    if settings_write:
        values[SETTINGS_WRITE_ENV] = SETTINGS_WRITE_SENTINEL
    return values


def _core(
    tmp_path: Path, *, live_adjust: bool = False, settings_write: bool = False
):
    fake = FakeBridgeClient("AA:BB")
    core = BridgeCore(
        default_address="AA:BB",
        state_dir=tmp_path,
        client_factory=lambda _address: fake,
        environ=_environment(
            live_adjust=live_adjust, settings_write=settings_write
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


def test_coffee_lifecycle_uses_one_held_client(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        connected = await core.rpc("connect")
        assert connected["connected"] is True
        assert "serial_number" not in core.machine_info

        loaded = await core.rpc("coffee.load", {"recipe": str(recipe)})
        assert loaded["status"] == "armed"
        assert core.coffee_state_file.exists()

        await core.rpc("coffee.start", {"confirmation": READY_SENTINEL})
        assert core.status()["phase"] == "running"
        await core.rpc("pause")
        assert core.status()["phase"] == "paused"
        await core.rpc("resume")
        assert core.status()["phase"] == "running"
        await core.rpc("cancel")
        assert core.status()["activity"] is None
        assert not core.coffee_state_file.exists()
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 1
    assert "coffee_pause" in fake.calls and "coffee_resume" in fake.calls


def test_coffee_terminal_during_pause_does_not_restore_stale_paused_state(tmp_path):
    core, fake = _core(tmp_path)
    fake.coffee_terminal_on_pause = True
    recipe = _recipe(tmp_path / "recipe.yaml")

    async def go():
        await core.rpc("coffee.load", {"recipe": str(recipe)})
        await core.rpc("coffee.start", {"confirmation": READY_SENTINEL})
        result = await core.rpc("pause")
        assert result["terminal_during_control"] is True
        assert core.status()["activity"] is None
        assert core.status()["phase"] == "idle"
        assert not core.coffee_state_file.exists()
        await core.shutdown()

    asyncio.run(go())


@pytest.mark.parametrize(
    ("kind", "expected_call"),
    [("coffee", "coffee_cancel"), ("tea", "tea_unload")],
)
def test_cancel_recovers_loaded_record_after_bridge_restart(
    tmp_path, kind, expected_call
):
    core, fake = _core(tmp_path)
    record = core.coffee_state_file if kind == "coffee" else core.tea_state_file
    record.write_text(
        json.dumps(
            {
                "address": "AA:BB",
                "status": "completion_unconfirmed",
                "owner": "bridge",
            }
        ),
        encoding="utf-8",
    )

    async def go():
        result = await core.rpc("cancel")
        assert result == {
            "status": "recovery_cancel_sent",
            "activity": kind,
            "record_cleared": True,
        }
        assert not record.exists()
        await core.shutdown()

    asyncio.run(go())
    assert expected_call in fake.calls


def test_tea_lifecycle_stays_on_held_connection_and_finishes_on_terminal(tmp_path):
    core, fake = _core(tmp_path)
    recipe = _tea_recipe(tmp_path / "tea.yaml")

    async def go():
        loaded = await core.rpc("tea.load", {"recipe": str(recipe)})
        assert loaded["status"] == "tea_loaded"
        assert core.tea_state_file.exists()

        started = await core.rpc(
            "tea.start", {"confirmation": TEA_READY_SENTINEL}
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
            "scale.start", {"duration_s": 10, "tare": False}
        )
        assert started["entry_auto_zero"] is True
        await asyncio.sleep(0)
        assert core.status()["activity"] == "scale"
        assert core.status()["telemetry"]["scale_g"] == 12.34
        tare = await core.rpc("scale.tare")
        assert tare["command_write_verified"] is True
        await core.rpc("cancel")
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
        written = await core.rpc(
            "settings.write",
            {
                "display": "high",
                "confirmation": SETTINGS_CONFIRM_SENTINEL,
            },
        )
        assert written["readback"] == {"display": "high"}

        advanced = await core.rpc("advanced.read")
        assert advanced["settings"]["pour_radius_level"] == 3
        tuned = await core.rpc(
            "advanced.write",
            {
                "pour_radius_level": 4,
                "vibration_level": 2,
                "confirmation": ADVANCED_CONFIRM_SENTINEL,
            },
        )
        assert tuned["readback"]["pour_radius"] == 760
        assert tuned["readback"]["vibration_amplitude"] == 1100

        saved = await core.rpc(
            "presets.save", {"recipes": [str(path) for path in recipes]}
        )
        assert saved["status"] == "saved"
        assert saved["brew_started"] is False
        await core.shutdown()

    asyncio.run(go())
    assert fake.calls.count("connect") == 1
    assert any(call[0] == "set_machine_settings" for call in fake.calls if isinstance(call, tuple))
    assert any(call[0] == "write_advanced_settings" for call in fake.calls if isinstance(call, tuple))
    assert any(call[0] == "save_slots" for call in fake.calls if isinstance(call, tuple))


def test_bridge_persistent_writes_keep_their_independent_gate(tmp_path):
    core, _fake = _core(tmp_path, settings_write=False)

    async def go():
        with pytest.raises(BridgeError, match="persistent machine writes disabled"):
            await core.rpc(
                "settings.write",
                {
                    "display": "high",
                    "confirmation": SETTINGS_CONFIRM_SENTINEL,
                },
            )

    asyncio.run(go())


def test_local_validation_and_recovery_records_block_before_ble_connect(tmp_path):
    core, fake = _core(tmp_path)
    core.coffee_state_file.write_text(
        json.dumps({"address": "AA:BB", "status": "armed"}),
        encoding="utf-8",
    )

    async def go():
        with pytest.raises(BridgeError, match="loaded workflow record exists"):
            await core.rpc("settings.read")
        assert fake.calls == []
        core.coffee_state_file.unlink()
        with pytest.raises(BridgeError, match="volume must be 20-360"):
            await core.rpc(
                "water.start",
                {
                    "volume_ml": 500,
                    "temp_c": 85,
                    "confirmation": WATER_READY_SENTINEL,
                },
            )
    asyncio.run(go())
    assert fake.calls == []


def test_freesolo_water_live_adjust_is_separately_gated(tmp_path):
    core, _fake = _core(tmp_path, live_adjust=False)

    async def blocked():
        await core.rpc(
            "water.start",
            {
                "volume_ml": 100,
                "temp_c": 20,
                "pattern": "center",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
        )
        with pytest.raises(BridgeError, match="not hardware A/B verified"):
            await core.rpc(
                "water.set_pattern",
                {"pattern": "spiral", "confirmation": LIVE_ADJUST_SENTINEL},
            )
        await core.rpc("cancel")
        await core.shutdown()

    asyncio.run(blocked())

    enabled, fake = _core(tmp_path / "enabled", live_adjust=True)

    async def allowed():
        await enabled.rpc(
            "water.start",
            {
                "volume_ml": 100,
                "temp_c": 20,
                "pattern": "center",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
        )
        await enabled.rpc("pause")
        temperature = await enabled.rpc(
            "water.set_temperature",
            {"temp_c": 60, "confirmation": LIVE_ADJUST_SENTINEL},
        )
        pattern = await enabled.rpc(
            "water.set_pattern",
            {"pattern": "spiral", "confirmation": LIVE_ADJUST_SENTINEL},
        )
        assert not temperature["hardware_effect_verified"]
        assert pattern["hardware_effect_verified"]
        assert pattern["report"] == 8107
        await enabled.rpc("resume")
        await enabled.rpc("cancel")
        await enabled.shutdown()

    asyncio.run(allowed())
    assert ("water_temperature", 60) in fake.calls
    assert ("water_pattern", "spiral") in fake.calls


def test_grinder_pause_extends_timer_and_stop_persists_cooldown(tmp_path):
    core, fake = _core(tmp_path)

    async def go():
        await core.rpc(
            "grinder.start",
            {
                "size": 60,
                "rpm": 100,
                "seconds": 0.15,
                "confirmation": GRINDER_READY_SENTINEL,
            },
        )
        await asyncio.sleep(0.03)
        await core.rpc("pause")
        await asyncio.sleep(0.18)
        assert core.status()["phase"] == "paused"
        await core.rpc("resume")
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
        await core.rpc(
            "grinder.start",
            {
                "size": 60,
                "rpm": 100,
                "seconds": 10,
                "confirmation": GRINDER_READY_SENTINEL,
            },
        )
        with pytest.raises(BridgeError, match="STOP/QUIT was confirmed"):
            await core.rpc("pause")
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
            {
                "volume_ml": 100,
                "temp_c": 85,
                "pattern": "center",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
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
            {
                "volume_ml": 100,
                "temp_c": 20,
                "pattern": "center",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
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
            {
                "volume_ml": 100,
                "temp_c": 85,
                "pattern": "circular",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
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
        await core.rpc(
            "water.start",
            {
                "volume_ml": 200,
                "temp_c": 20,
                "pattern": "center",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
        )
        fake.emit(_event(command=40523, name="water_volume", water_ml=97.65))
        fake.emit(_event(command=40523, name="water_volume", water_ml=0.0))
        result = await core.rpc("cancel")
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
            {
                "volume_ml": 100,
                "temp_c": 85,
                "pattern": "center",
                "water_source": "tank",
                "confirmation": WATER_READY_SENTINEL,
            },
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

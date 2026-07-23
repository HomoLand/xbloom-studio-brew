import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import xbloom
import xbloom_ble.bridge as bridge_module


def test_runtime_reexec_preserves_callers_working_directory(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime-python.exe"
    runtime.touch()
    caller = tmp_path / "workspace"
    caller.mkdir()
    captured = {}

    monkeypatch.chdir(caller)
    monkeypatch.delenv("XBLOOM_SKILL_REEXEC", raising=False)
    monkeypatch.setattr(xbloom, "local_python", lambda: runtime)
    monkeypatch.setattr(
        xbloom.sys, "argv", ["xbloom.py", "validate", "recipe.yaml"]
    )

    def fake_call(command, *, env):
        captured["command"] = command
        captured["env"] = env
        captured["cwd"] = Path.cwd()
        return 0

    monkeypatch.setattr(xbloom.subprocess, "call", fake_call)

    with pytest.raises(SystemExit) as exc:
        xbloom.reexec_in_local_runtime()

    assert exc.value.code == 0
    assert captured["cwd"] == caller
    assert captured["command"][-1] == "recipe.yaml"
    assert captured["env"]["XBLOOM_SKILL_REEXEC"] == "1"


def test_emit_falls_back_to_ascii_for_legacy_windows_console(monkeypatch):
    class NarrowConsole:
        encoding = "gbk"

        def __init__(self):
            self.text = ""

        def write(self, value):
            value.encode(self.encoding)
            self.text += value
            return len(value)

        def flush(self):
            return None

    stream = NarrowConsole()
    monkeypatch.setattr(xbloom.sys, "stdout", stream)
    xbloom.emit({"device": "XBLOOM \ufffd"})
    assert "\\ufffd" in stream.text


def test_save_slots_parser_accepts_exactly_three_recipes():
    args = xbloom.build_parser().parse_args(["save-slots", "a.yaml", "b.yaml", "c.yaml"])
    assert args.recipes == ["a.yaml", "b.yaml", "c.yaml"]
    assert args.scale == ("on", "on", "on")
    configured = xbloom.build_parser().parse_args(
        [
            "save-slots",
            "a.yaml",
            "b.yaml",
            "c.yaml",
            "--scale",
            "on",
            "off",
            "on",
        ]
    )
    assert configured.scale == ["on", "off", "on"]


def test_catalog_and_slot_validation_parsers_are_offline_capable():
    parser = xbloom.build_parser()
    validate = parser.parse_args(["validate", "recipe.yaml", "--slot"])
    assert validate.slot is True
    status = parser.parse_args(
        ["catalog", "--catalog-file", "catalog.json", "status"]
    )
    assert (status.catalog_action, status.catalog_file) == ("status", "catalog.json")
    listing = parser.parse_args(
        ["catalog", "list", "--kind", "tea", "--slot-compatible"]
    )
    assert listing.kind == "tea" and listing.slot_compatible is True
    sync = parser.parse_args(
        ["catalog", "sync", "--config", "cloud.json", "--include", "coffee"]
    )
    assert sync.include == ["coffee"]
    login_sync = parser.parse_args(
        [
            "catalog",
            "login-sync",
            "--region",
            "international",
            "--language",
            "zh-cn",
            "--include",
            "tea",
        ]
    )
    assert login_sync.email is None
    assert login_sync.language == "zh-cn"
    assert login_sync.include == ["tea"]
    push = parser.parse_args(
        [
            "catalog",
            "push",
            "tea.yaml",
            "--region",
            "china",
            "--apply",
            "--confirm-write",
            "own-account-cloud-recipe",
        ]
    )
    assert push.recipe == "tea.yaml"
    assert push.apply is True
    assert push.confirm_write == "own-account-cloud-recipe"


def test_freesolo_and_tea_parsers_expose_guarded_parameters():
    parser = xbloom.build_parser()
    default_scale = parser.parse_args(["scale"])
    assert default_scale.tare is False
    scale = parser.parse_args(["scale", "--tare", "--duration", "5"])
    assert scale.tare and scale.duration == 5
    grind = parser.parse_args(
        ["grind", "--size", "62", "--rpm", "100", "--seconds", "10"]
    )
    assert (grind.size, grind.rpm, grind.seconds, grind.confirm_ready) == (62, 100, 10, "")
    water = parser.parse_args(["water", "--volume", "250", "--temp", "85"])
    assert (water.volume, water.temp, water.pattern, water.water_source) == (
        250,
        85,
        "center",
        "auto",
    )
    water_tap = parser.parse_args(
        ["water", "--volume", "250", "--temp", "85", "--water-source", "tap"]
    )
    assert water_tap.water_source == "tap"
    water_rt = parser.parse_args(["water", "--volume", "250", "--temp", "RT"])
    assert water_rt.temp == xbloom.ROOM_TEMPERATURE_C == 20
    water_rt_lower = parser.parse_args(["water", "--volume", "250", "--temp", "rt"])
    assert water_rt_lower.temp == xbloom.ROOM_TEMPERATURE_C
    tea = parser.parse_args(["tea-start", "green.yaml"])
    assert tea.recipe == "green.yaml" and tea.confirm_ready == ""
    tea_brew = parser.parse_args(["tea-brew", "green.yaml"])
    assert tea_brew.recipe == "green.yaml" and tea_brew.confirm_ready == ""

    settings = parser.parse_args(
        [
            "set-settings",
            "--weight-unit",
            "g",
            "--temperature-unit",
            "C",
            "--water-source",
            "tap",
            "--display",
            "high",
            "--confirm-write",
            xbloom.SETTINGS_CONFIRM_SENTINEL,
        ]
    )
    assert (settings.weight_unit, settings.temperature_unit) == ("g", "C")
    assert (settings.water_source, settings.display) == ("tap", "high")

    advanced = parser.parse_args(
        [
            "set-advanced",
            "--pour-radius-level",
            "2",
            "--vibration-level",
            "4",
            "--confirm-write",
            xbloom.ADVANCED_CONFIRM_SENTINEL,
        ]
    )
    assert (advanced.pour_radius_level, advanced.vibration_level) == (2, 4)


def test_bridge_parser_exposes_interactive_controls():
    parser = xbloom.build_parser()
    assert parser.parse_args(["bridge", "start"]).bridge_action == "start"
    water = parser.parse_args(
        ["bridge", "water-start", "--volume", "100", "--temp", "RT"]
    )
    assert (water.volume, water.temp, water.pattern) == (100, 20, "center")
    adjust = parser.parse_args(
        ["bridge", "water-pattern", "--pattern", "spiral"]
    )
    assert adjust.bridge_action == "water-pattern"
    scale = parser.parse_args(["bridge", "scale-start", "--duration", "5"])
    assert (scale.bridge_action, scale.duration, scale.tare) == (
        "scale-start",
        5,
        False,
    )
    tea = parser.parse_args(["bridge", "tea-load", "tea.yaml"])
    assert (tea.bridge_action, tea.recipe) == ("tea-load", "tea.yaml")
    slots = parser.parse_args(
        [
            "bridge",
            "save-slots",
            "a.yaml",
            "b.yaml",
            "c.yaml",
            "--scale",
            "off",
            "on",
            "off",
        ]
    )
    assert slots.recipes == ["a.yaml", "b.yaml", "c.yaml"]
    assert slots.scale == ["off", "on", "off"]
    settings = parser.parse_args(
        ["bridge", "set-settings", "--display", "high"]
    )
    assert (settings.bridge_action, settings.display) == ("set-settings", "high")


@pytest.mark.parametrize("value", ["20", "39", "99", "room"])
def test_water_parser_rejects_numeric_or_ambiguous_non_rt_values(value):
    with pytest.raises(SystemExit):
        xbloom.build_parser().parse_args(["water", "--volume", "120", "--temp", value])


def test_physical_tools_are_disabled_before_any_ble_resolution(monkeypatch):
    monkeypatch.delenv(xbloom.REMOTE_GRINDER_ENV, raising=False)
    grind = xbloom.build_parser().parse_args(
        ["grind", "--size", "62", "--seconds", "10", "--confirm-ready", xbloom.GRINDER_READY_SENTINEL]
    )
    with pytest.raises(RuntimeError, match="remote grinder disabled"):
        asyncio.run(xbloom.async_grind(grind))

    monkeypatch.delenv(xbloom.REMOTE_START_ENV, raising=False)
    water = xbloom.build_parser().parse_args(
        ["water", "--volume", "250", "--temp", "85", "--confirm-ready", xbloom.WATER_READY_SENTINEL]
    )
    with pytest.raises(RuntimeError, match="hot-water actions disabled"):
        asyncio.run(xbloom.async_water(water))

    tea_brew = xbloom.build_parser().parse_args(
        [
            "tea-brew",
            "green.yaml",
            "--confirm-ready",
            xbloom.TEA_READY_SENTINEL,
        ]
    )
    with pytest.raises(RuntimeError, match="hot-water actions disabled"):
        asyncio.run(xbloom.async_tea_brew(tea_brew))


def test_persistent_setting_writes_require_owner_and_per_call_gates(monkeypatch):
    monkeypatch.delenv(xbloom.SETTINGS_WRITE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="persistent machine writes disabled"):
        xbloom.require_settings_write_gate(
            xbloom.SETTINGS_CONFIRM_SENTINEL, xbloom.SETTINGS_CONFIRM_SENTINEL
        )

    monkeypatch.setenv(
        xbloom.SETTINGS_WRITE_ENV, xbloom.SETTINGS_WRITE_SENTINEL
    )
    with pytest.raises(RuntimeError, match="--confirm-write"):
        xbloom.require_settings_write_gate("wrong", xbloom.SETTINGS_CONFIRM_SENTINEL)
    xbloom.require_settings_write_gate(
        xbloom.SETTINGS_CONFIRM_SENTINEL, xbloom.SETTINGS_CONFIRM_SENTINEL
    )


def test_grinder_rest_interval_is_persisted(monkeypatch, tmp_path):
    path = tmp_path / "grinder.json"
    monkeypatch.setattr(xbloom, "GRINDER_STATE_FILE", path)
    xbloom.reserve_grinder_rest(10)
    with pytest.raises(RuntimeError, match="rest interval active"):
        xbloom.require_grinder_rest()


def test_corrupt_grinder_rest_record_blocks_motor(monkeypatch, tmp_path):
    path = tmp_path / "grinder.json"
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(xbloom, "GRINDER_STATE_FILE", path)
    with pytest.raises(RuntimeError, match="rest record is unreadable"):
        xbloom.require_grinder_rest()


def test_unverified_bridge_grinder_stop_record_blocks_motor(monkeypatch, tmp_path):
    path = tmp_path / "grinder.json"
    path.write_text('{"in_progress": true}', encoding="utf-8")
    monkeypatch.setattr(xbloom, "GRINDER_STATE_FILE", path)
    with pytest.raises(RuntimeError, match="no verified stop"):
        xbloom.require_grinder_rest()


def test_hardware_commands_no_longer_refuse_running_bridge(monkeypatch):
    """A9: hardware commands use the daemon; they must not refuse a running bridge."""

    monkeypatch.setattr(bridge_module, "bridge_is_running", lambda: True)
    # ensure_bridge_not_running is removed; validate remains non-hardware.
    assert not hasattr(xbloom, "ensure_bridge_not_running") or not callable(
        getattr(xbloom, "DIRECT_BLE_COMMANDS", None)
    )
    assert not hasattr(xbloom, "DIRECT_BLE_COMMANDS")


def test_doctor_reports_unverified_live_adjust_gate(monkeypatch, capsys):
    monkeypatch.setenv(xbloom.LIVE_ADJUST_ENV, xbloom.LIVE_ADJUST_SENTINEL)
    monkeypatch.setattr(bridge_module, "bridge_is_running", lambda: False)
    assert xbloom.cmd_doctor(SimpleNamespace(scan=False, scan_timeout=0.1)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["physical_actions_enabled"]["live_adjust_unverified"] is True
    assert report["capabilities"]["freesolo_live_adjust_hardware_verified"] is False
    assert report["capabilities"]["freesolo_live_pattern_hardware_verified"] is True
    assert (
        report["capabilities"]["freesolo_live_temperature_hardware_verified"]
        is False
    )


def test_doctor_reports_catalog_configuration_without_reading_secrets(
    monkeypatch, tmp_path, capsys
):
    config = tmp_path / "cloud.json"
    config.write_text('{"token":"do-not-print"}', encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    monkeypatch.setenv(xbloom.CLOUD_CONFIG_ENV, str(config))
    monkeypatch.setenv(xbloom.CATALOG_PATH_ENV, str(catalog))
    monkeypatch.setenv(xbloom.ACCOUNT_EMAIL_ENV, "private@example.test")
    monkeypatch.setenv(xbloom.ACCOUNT_PASSWORD_ENV, "do-not-print-password")
    monkeypatch.setattr(bridge_module, "bridge_is_running", lambda: False)
    assert xbloom.cmd_doctor(SimpleNamespace(scan=False, scan_timeout=0.1)) == 0
    report = json.loads(capsys.readouterr().out)
    # Doctor honestly reports state.db as the catalog runtime path.
    assert report["capabilities"]["catalog_path"] == str(tmp_path / "state.db")
    assert report["capabilities"]["catalog_source"] == "state.db"
    assert report["capabilities"]["catalog_cloud_configured"] is True
    assert report["capabilities"]["catalog_login_configured"] is True
    assert report["capabilities"]["catalog_login_email_configured"] is True
    assert report["capabilities"]["catalog_login_password_configured"] is True
    assert "do-not-print" not in json.dumps(report)
    assert "private@example.test" not in json.dumps(report)


def test_supported_firmware_passes_preflight(monkeypatch):
    monkeypatch.delenv(xbloom.UNTESTED_FIRMWARE_ENV, raising=False)
    assert xbloom.require_write_preflight(
        {"firmware": ["V12.0D.500"], "states": ["loading", "idle"]}
    ) == "V12.0D.500"


def test_machine_info_redacts_serial_but_keeps_operational_settings():
    public = xbloom.redact_machine_info(
        {
            "serial_number": "private",
            "firmware": "V12.0D.500",
            "water_source": "tank",
        }
    )
    assert "serial_number" not in public
    assert public == {"firmware": "V12.0D.500", "water_source": "tank"}


@pytest.mark.parametrize("firmware", [[], ["V99.0A.1"]])
def test_unknown_firmware_is_blocked(monkeypatch, firmware):
    monkeypatch.delenv(xbloom.UNTESTED_FIRMWARE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="not in the tested set"):
        xbloom.require_write_preflight({"firmware": firmware, "states": ["idle"]})


def test_unknown_firmware_requires_exact_owner_override(monkeypatch):
    monkeypatch.setenv(xbloom.UNTESTED_FIRMWARE_ENV, "yes")
    with pytest.raises(RuntimeError, match="not in the tested set"):
        xbloom.require_write_preflight({"firmware": ["V99.0A.1"], "states": ["idle"]})
    monkeypatch.setenv(xbloom.UNTESTED_FIRMWARE_ENV, xbloom.UNTESTED_FIRMWARE_SENTINEL)
    assert xbloom.require_write_preflight(
        {"firmware": ["V99.0A.1"], "states": ["idle"]}
    ) == "V99.0A.1"


@pytest.mark.parametrize("state", ["armed", "awaiting_confirm", "starting", "brewing", "saving_slots"])
def test_active_machine_state_blocks_writes(monkeypatch, state):
    monkeypatch.setenv(xbloom.UNTESTED_FIRMWARE_ENV, xbloom.UNTESTED_FIRMWARE_SENTINEL)
    with pytest.raises(RuntimeError, match="machine is not idle"):
        xbloom.require_write_preflight({"firmware": ["V12.0D.500"], "states": [state]})

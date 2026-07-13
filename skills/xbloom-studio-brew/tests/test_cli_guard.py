import asyncio

import pytest

import xbloom


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

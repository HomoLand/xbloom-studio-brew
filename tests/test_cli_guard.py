import pytest

import xbloom


def test_save_slots_parser_accepts_exactly_three_recipes():
    args = xbloom.build_parser().parse_args(["save-slots", "a.yaml", "b.yaml", "c.yaml"])
    assert args.recipes == ["a.yaml", "b.yaml", "c.yaml"]


def test_supported_firmware_passes_preflight(monkeypatch):
    monkeypatch.delenv(xbloom.UNTESTED_FIRMWARE_ENV, raising=False)
    assert xbloom.require_write_preflight(
        {"firmware": ["V12.0D.500"], "states": ["loading", "idle"]}
    ) == "V12.0D.500"


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

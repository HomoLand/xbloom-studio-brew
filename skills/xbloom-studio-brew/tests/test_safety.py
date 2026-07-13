from pathlib import Path

import pytest
import yaml

from xbloom_ble.recipe import RecipeError
from xbloom_safety import (
    SafetyError,
    load_strict_recipe,
    recipe_summary,
    validate_slot_compatible,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("asset", ["hot-template.yaml", "flash-brew-template.yaml"])
def test_bundled_templates_pass_strict_validation(asset):
    path = ROOT / "assets" / asset
    recipe = load_strict_recipe(path)
    summary = recipe_summary(recipe, path)
    assert summary["load_opcodes"]
    assert not ({"0x42", "0x46", "0x47"} & set(summary["load_opcodes"]))


def _hot_mapping():
    return yaml.safe_load((ROOT / "assets" / "hot-template.yaml").read_text(encoding="utf-8"))


def _write(tmp_path, data):
    path = tmp_path / "recipe.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_raw_protocol_override_is_rejected(tmp_path):
    data = _hot_mapping()
    data["opcode"] = "0x46"
    with pytest.raises(SafetyError, match="raw protocol overrides"):
        load_strict_recipe(_write(tmp_path, data))


def test_large_pour_is_rejected_even_if_upstream_can_split(tmp_path):
    data = _hot_mapping()
    data["pours"] = [
        {**data["pours"][0], "ml": 45},
        {**data["pours"][1], "ml": 135},
        {**data["pours"][2], "ml": 60},
    ]
    with pytest.raises(SafetyError, match="10-127 ml"):
        load_strict_recipe(_write(tmp_path, data))


def test_inconsistent_recipe_rpm_is_rejected(tmp_path):
    data = _hot_mapping()
    data["pours"][1]["rpm"] = 100
    with pytest.raises(SafetyError, match="repeat one grinder rpm"):
        load_strict_recipe(_write(tmp_path, data))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dose_g", 15.5, "whole grams"),
        ("grind", 58.5, "whole-number Studio setting"),
    ],
)
def test_fractional_machine_settings_are_rejected_not_truncated(
    tmp_path, field, value, message
):
    data = _hot_mapping()
    data[field] = value
    if field == "dose_g":
        data.pop("ratio")
    with pytest.raises(RecipeError, match=message):
        load_strict_recipe(_write(tmp_path, data))


def test_center_first_pour_is_rejected(tmp_path):
    data = _hot_mapping()
    data["pours"][0]["pattern"] = "center"
    data["pours"][0]["vibration"] = "none"
    data["pours"][0]["rpm"] = 0
    with pytest.raises(SafetyError, match="first pour cannot use center"):
        load_strict_recipe(_write(tmp_path, data))


def test_flash_metadata_must_balance(tmp_path):
    data = yaml.safe_load(
        (ROOT / "assets" / "flash-brew-template.yaml").read_text(encoding="utf-8")
    )
    data["ice_g"] = 80
    with pytest.raises(SafetyError, match="water_ml must equal"):
        load_strict_recipe(_write(tmp_path, data))


def test_unknown_recipe_key_is_rejected(tmp_path):
    data = _hot_mapping()
    data["download_from"] = "https://example.invalid/recipe.yaml"
    with pytest.raises(SafetyError, match="unknown top-level"):
        load_strict_recipe(_write(tmp_path, data))


def test_guard_accepts_app_bypass_rt_and_pre_ground(tmp_path):
    data = _hot_mapping()
    data["grind"] = 0
    data["bypass_ml"] = 30
    data["bypass_temp_c"] = "RT"
    data["water_ml"] = int(data["hot_water_ml"]) + 30
    recipe = load_strict_recipe(_write(tmp_path, data))
    assert recipe.no_grind
    assert recipe.bypass_temp_c == 20


def test_guard_accepts_app_numeric_bypass_temperature_range(tmp_path):
    data = _hot_mapping()
    data["bypass_ml"] = 30
    data["bypass_temp_c"] = 60
    data["water_ml"] = int(data["hot_water_ml"]) + 30
    assert load_strict_recipe(_write(tmp_path, data)).bypass_temp_c == 60


def test_guard_accepts_official_low_temperature_iced_pour(tmp_path):
    data = _hot_mapping()
    data["pours"][0]["temp_c"] = 71
    assert load_strict_recipe(_write(tmp_path, data)).pours[0].temp_c == 71


def test_slot_guard_rejects_recipe_bypass_instead_of_dropping_it(tmp_path):
    data = _hot_mapping()
    data["bypass_ml"] = 30
    data["bypass_temp_c"] = "RT"
    data["water_ml"] = int(data["hot_water_ml"]) + 30
    recipe = load_strict_recipe(_write(tmp_path, data))
    with pytest.raises(SafetyError, match="cannot represent bypass"):
        validate_slot_compatible(recipe)

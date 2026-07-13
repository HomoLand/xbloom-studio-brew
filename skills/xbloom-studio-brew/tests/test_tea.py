from pathlib import Path

import pytest

from xbloom_ble.tea import TeaRecipe, TeaRecipeError


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name,temp,pauses,programmed_ratio",
    [
        ("tea-green-official.yaml", 85, [20, 15], 45.0),
        ("tea-white-official.yaml", 99, [30, 30], 45.0),
        ("tea-flower-official.yaml", 90, [30, 20], 45.0),
        ("tea-black-official.yaml", 99, [30, 25], 45.0),
        ("tea-oolong-official.yaml", 99, [15, 10], 45.0),
    ],
)
def test_official_tea_assets_validate(name, temp, pauses, programmed_ratio):
    recipe = TeaRecipe.from_yaml(ROOT / "assets" / name)
    assert recipe.leaf_g == 4
    assert recipe.output_ml_per_steep == 120
    assert [pour.temp_c for pour in recipe.pours] == [temp, temp]
    assert [pour.pause_s for pour in recipe.pours] == pauses
    assert recipe.to_protocol_dict()["cup_max_mm"] == 80.0
    assert recipe.to_protocol_dict()["grand_water"] == programmed_ratio
    assert recipe.summary()["approx_finished_output_ml"] == 240
    assert recipe.summary()["finish_phase"] == "firmware-managed-siphon"


def test_tea_rejects_unknown_protocol_knobs():
    with pytest.raises(TeaRecipeError, match="unknown tea recipe keys"):
        TeaRecipe.from_dict(
            {
                "name": "unsafe",
                "kind": "tea",
                "leaf_g": 4,
                "output_ml_per_steep": 120,
                "raw": "deadbeef",
                "pours": [
                    {"ml": 90, "temp_c": 85, "pattern": "ring", "pause_s": 20, "flow_ml_s": 3.5}
                ],
            }
        )


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("leaf_g", 8, "leaf_g"),
        ("output_ml_per_steep", 300, "output_ml_per_steep"),
    ],
)
def test_tea_guarded_ranges(field, value, message):
    data = {
        "name": "test",
        "kind": "tea",
        "leaf_g": 4,
        "output_ml_per_steep": 120,
        "pours": [
            {"ml": 90, "temp_c": 85, "pattern": "ring", "pause_s": 20, "flow_ml_s": 3.5}
        ],
    }
    data[field] = value
    with pytest.raises(TeaRecipeError, match=message):
        TeaRecipe.from_dict(data)

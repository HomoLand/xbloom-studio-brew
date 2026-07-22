"""Strict recipe and opcode safety policy for the portable xBloom skill."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from xbloom_ble.protocol import build_load_frames
from xbloom_ble.recipe import Recipe, RecipeError


BREW_CONTROL_OPCODES = frozenset({0x42, 0x46, 0x47})
LOAD_ONLY_OPCODES = frozenset({0xA4, 0xA6, 0xA8, 0x41, 0x44})
ALLOWED_TOP_LEVEL = frozenset(
    {
        "name",
        "dose_g",
        "grind",
        "ratio",
        # Deprecated read-only compatibility input. Recipe.from_dict accepts only
        # the captured 110/90 command-8104 pair and omits it when serialising.
        "stage_temps",
        "bypass_ml",
        "bypass_temp_c",
        "dripper",
        "kind",
        "water_ml",
        "hot_water_ml",
        "ice_g",
        "time",
        "note",
        "pours",
    }
)
ALLOWED_POUR_KEYS = frozenset(
    {
        "label",
        "ml",
        "temp_c",
        "pattern",
        "vibration",
        "agitation",
        "pause_s",
        "rpm",
        "flow_ml_s",
    }
)
FORBIDDEN_PROTOCOL_KEYS = frozenset({"tail", "seq", "opcode", "raw", "frame"})


class SafetyError(RecipeError):
    """Raised when a device-valid recipe violates the skill's safer envelope."""


def recipe_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_mapping(path: Path) -> dict[str, Any]:
    if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise SafetyError("recipe must be a local .yaml, .yml, or .json file")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SafetyError("recipe must contain one mapping")
    forbidden = FORBIDDEN_PROTOCOL_KEYS & set(data)
    if forbidden:
        raise SafetyError(f"raw protocol overrides are forbidden: {sorted(forbidden)}")
    unknown = set(data) - ALLOWED_TOP_LEVEL
    if unknown:
        raise SafetyError(f"unknown top-level recipe keys: {sorted(unknown)}")
    pours = data.get("pours")
    if isinstance(pours, list):
        for index, pour in enumerate(pours, start=1):
            if not isinstance(pour, dict):
                continue
            unknown_pour = set(pour) - ALLOWED_POUR_KEYS
            if unknown_pour:
                raise SafetyError(f"pour {index} has unknown keys: {sorted(unknown_pour)}")
    return data


def load_strict_recipe(path: str | Path) -> Recipe:
    resolved = Path(path).expanduser().resolve(strict=True)
    data = _read_mapping(resolved)
    recipe = Recipe.from_dict(data)
    strict_validate(recipe)
    return recipe


def strict_validate(recipe: Recipe) -> None:
    """Apply conservative Omni Dripper limits before any BLE write."""
    recipe.validate()
    errors: list[str] = []
    kind = (recipe.kind or "hot").strip().lower()
    if kind in {"iced", "flash", "japanese-iced", "japanese-iced-coffee"}:
        kind = "flash-brew"
    if kind not in {"hot", "flash-brew"}:
        errors.append("kind must be 'hot' or 'flash-brew'")

    if isinstance(recipe.dose_g, bool) or not float(recipe.dose_g).is_integer():
        errors.append("dose_g must use whole grams")
    if not 5 <= int(recipe.dose_g) <= 18:
        errors.append("dose_g must be 5-18 g")
    if isinstance(recipe.grind, bool) or not float(recipe.grind).is_integer():
        errors.append("grind must use a whole-number Studio setting")
    if int(recipe.grind) != 0 and not 35 <= int(recipe.grind) <= 75:
        errors.append("grind must be 35-75, or 0 for pre-ground/grinder-off")
    if not 2 <= len(recipe.pours) <= 5:
        errors.append("pour count must be 2-5")
    if recipe.dripper and "omni" not in recipe.dripper.lower():
        errors.append("the guarded controller currently supports an Omni Dripper only")

    total_hot = recipe.total_water_ml
    bypass_ml = float(recipe.bypass_ml or 0.0)
    total_machine_water = total_hot + bypass_ml
    machine_ratio = total_hot / float(recipe.dose_g)
    if not 60 <= total_machine_water <= 360:
        errors.append("total machine water (pours + bypass) must be 60-360 ml")
    if bypass_ml and int(recipe.bypass_temp_c) not in {20, 98} and not (
        40 <= int(recipe.bypass_temp_c) <= 95
    ):
        errors.append("bypass temperature must be RT, 40-95 C, or BP")

    legacy_agitation_count = 0
    non_center_rpms: set[int] = set()
    for index, pour in enumerate(recipe.pours, start=1):
        if not 10 <= int(pour.ml) <= 127:
            errors.append(f"pour {index} must be 10-127 ml; protocol auto-splitting is disabled")
        if int(pour.temp_c) not in {20, 98} and not 40 <= int(pour.temp_c) <= 95:
            errors.append(f"pour {index} temperature must be RT, 40-95 C, or BP")
        if not 0 <= int(pour.pause_s) <= 60:
            errors.append(f"pour {index} pause must be 0-60 s")
        flow10 = round(float(pour.flow_ml_s) * 10)
        if flow10 not in range(30, 36) or abs(flow10 / 10 - float(pour.flow_ml_s)) > 1e-6:
            errors.append(f"pour {index} flow must be 3.0-3.5 ml/s in 0.1 steps")
        rpm = int(pour.rpm)
        if pour.pattern == "center":
            if rpm != 0:
                errors.append(f"pour {index} center pattern requires rpm 0")
        elif rpm not in {60, 70, 80, 90, 100, 110, 120}:
            errors.append(f"pour {index} rpm must be 60-120 in 10-rpm steps")
        else:
            non_center_rpms.add(rpm)
        if pour.agitation:
            legacy_agitation_count += 1
            if index != 1:
                errors.append("legacy agitation is allowed on the first pour only")
    if legacy_agitation_count > 1:
        errors.append("legacy agitation is allowed on at most one pour")
    if recipe.pours and recipe.pours[0].pattern == "center":
        errors.append("the first pour cannot use center; the decoded schema carries grinder RPM there")
    if len(non_center_rpms) > 1:
        errors.append("repeat one grinder rpm across every non-center pour")

    if kind == "hot":
        if bypass_ml:
            if not 8 <= machine_ratio <= 20:
                errors.append("hot bypass extraction ratio must be 1:8 through 1:20")
            if not 12 <= total_machine_water / float(recipe.dose_g) <= 20:
                errors.append("hot bypass final water ratio must be 1:12 through 1:20")
        elif not 12 <= machine_ratio <= 20:
            errors.append(
                "local kind=hot final serving ratio must be 1:12 through 1:20; "
                "xBloom has no separate flash program, so a concentrated cloud "
                "program may instead need confirmed local ice metadata"
            )
        if recipe.ice_g not in (None, 0):
            errors.append(
                "local kind=hot cannot declare ice_g; use kind=flash-brew only "
                "for manual vessel-ice metadata, not as a machine mode"
            )
        if recipe.hot_water_ml is not None and int(recipe.hot_water_ml) != total_hot:
            errors.append("hot_water_ml must equal the extraction-pour total")
        if recipe.water_ml is not None and int(recipe.water_ml) != total_machine_water:
            errors.append("water_ml must equal pours + bypass for hot recipes")

    if kind == "flash-brew":
        if not 8 <= machine_ratio <= 14:
            errors.append("flash-brew machine hot-water ratio must be 1:8 through 1:14")
        if recipe.hot_water_ml is None or int(recipe.hot_water_ml) != total_hot:
            errors.append("flash-brew hot_water_ml must equal the sum of pours")
        if recipe.ice_g is None or not 40 <= int(recipe.ice_g) <= 180:
            errors.append("flash-brew ice_g must be 40-180 g")
        if recipe.water_ml is None or recipe.ice_g is None:
            errors.append("flash-brew requires water_ml, hot_water_ml, and ice_g")
        elif int(recipe.water_ml) != total_machine_water + int(recipe.ice_g):
            errors.append("flash-brew water_ml must equal pours + bypass + ice_g")
        elif not 12 <= int(recipe.water_ml) / float(recipe.dose_g) <= 20:
            errors.append("flash-brew final water ratio must be 1:12 through 1:20")

    frames = build_load_frames(recipe.to_protocol_dict())
    opcodes = {frame[3] for frame in frames}
    if opcodes & BREW_CONTROL_OPCODES:
        errors.append(f"load frames contain brew-control opcodes: {sorted(opcodes & BREW_CONTROL_OPCODES)}")
    if not opcodes <= LOAD_ONLY_OPCODES:
        errors.append(f"load frames contain unknown opcodes: {sorted(opcodes - LOAD_ONLY_OPCODES)}")

    if errors:
        raise SafetyError("; ".join(errors))


def validate_slot_compatible(recipe: Recipe) -> None:
    """Require semantics that Easy/Auto A/B/C can store without loss.

    Command 11510 stores the coffee pours, grinder setting, and the derived
    water-to-coffee ratio. Unlike the normal LOAD path, it has no command-8102
    dose/bypass block. The machine measures the dose when an Auto preset runs
    and scales the stored program from its ratio, but it cannot reproduce a
    post-brew bypass. Reject that mismatch instead of silently dropping water.
    """

    strict_validate(recipe)
    if float(recipe.bypass_ml or 0.0):
        raise SafetyError(
            "A/B/C Easy Mode cannot represent bypass_ml/bypass_temp_c; "
            "use a non-bypass preset or load this recipe in Pro mode"
        )


def recipe_summary(recipe: Recipe, path: str | Path) -> dict[str, Any]:
    return {
        "name": recipe.name,
        "kind": (recipe.kind or "hot"),
        "machine_program": "coffee-pour-over",
        "machine_dispenses_ice": False,
        "manual_preload_ice_g": int(recipe.ice_g or 0),
        "dose_g": int(recipe.dose_g),
        "grind": int(recipe.grind),
        "hot_water_ml": recipe.total_water_ml,
        "bypass_ml": float(recipe.bypass_ml or 0.0),
        "target_dispensed_water_ml": recipe.total_machine_water_ml,
        "bypass_temp_c": recipe.bypass_temp_c,
        "final_water_ml": int(
            recipe.water_ml
            or (recipe.total_machine_water_ml + int(recipe.ice_g or 0))
        ),
        "ice_g": int(recipe.ice_g or 0),
        "pours": len(recipe.pours),
        "recipe_sha256": recipe_sha256(path),
        "load_opcodes": [f"0x{frame[3]:02x}" for frame in build_load_frames(recipe.to_protocol_dict())],
    }

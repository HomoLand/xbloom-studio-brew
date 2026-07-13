"""Guarded Omni Tea Brewer recipe model.

Tea recipes use xBloom Studio's dedicated siphon-brewer command path. They are
not coffee recipes with the grinder disabled: the firmware receives a special
tea recipe blob and interprets each pour as one steep/siphon cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = ["TeaPour", "TeaRecipe", "TeaRecipeError"]


class TeaRecipeError(ValueError):
    """Raised when a tea recipe is malformed or outside the guarded envelope."""


TOP_LEVEL_KEYS = frozenset(
    {"name", "kind", "leaf_g", "output_ml_per_steep", "pours"}
)
POUR_KEYS = frozenset({"label", "ml", "temp_c", "pattern", "pause_s", "flow_ml_s"})
PATTERNS = frozenset({"center", "spiral", "circular", "ring"})


@dataclass(frozen=True)
class TeaPour:
    """One programmed steep/siphon cycle."""

    ml: int
    temp_c: int
    pattern: str = "circular"
    pause_s: int = 20
    flow_ml_s: float = 3.5
    label: str | None = None

    def to_protocol_dict(self) -> dict[str, Any]:
        return {
            "ml": int(self.ml),
            "temp": int(self.temp_c),
            "pattern": self.pattern,
            "pause": int(self.pause_s),
            "flow": float(self.flow_ml_s),
            # The official tea recipes have both vibration flags disabled.
            "vibration": 0,
        }


@dataclass(frozen=True)
class TeaRecipe:
    """A guarded xBloom Omni Tea Brewer recipe.

    ``leaf_g`` and ``output_ml_per_steep`` guide physical setup and reporting;
    the machine's tea blob itself contains only the ordered pour stages. The
    protocol suffix values below are intentionally fixed to the values present
    in all five official xBloom templates recovered from their public shares.
    """

    name: str
    leaf_g: float
    output_ml_per_steep: int
    pours: tuple[TeaPour, ...]
    kind: str = "tea"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeaRecipe":
        if not isinstance(data, dict):
            raise TeaRecipeError("tea recipe must be a mapping")
        unknown = set(data) - TOP_LEVEL_KEYS
        if unknown:
            raise TeaRecipeError(f"unknown tea recipe keys: {sorted(unknown)}")
        if str(data.get("kind", "tea")).strip().lower() != "tea":
            raise TeaRecipeError("tea recipe kind must be 'tea'")
        for key in ("leaf_g", "output_ml_per_steep", "pours"):
            if key not in data:
                raise TeaRecipeError(f"tea recipe is missing required key '{key}'")
        raw_pours = data["pours"]
        if not isinstance(raw_pours, list) or not raw_pours:
            raise TeaRecipeError("tea recipe pours must be a non-empty list")

        pours: list[TeaPour] = []
        for index, raw in enumerate(raw_pours, start=1):
            if not isinstance(raw, dict):
                raise TeaRecipeError(f"tea pour {index} must be a mapping")
            unknown_pour = set(raw) - POUR_KEYS
            if unknown_pour:
                raise TeaRecipeError(
                    f"tea pour {index} has unknown keys: {sorted(unknown_pour)}"
                )
            try:
                pours.append(
                    TeaPour(
                        ml=int(raw["ml"]),
                        temp_c=int(raw["temp_c"]),
                        pattern=(
                            "circular"
                            if str(raw.get("pattern", "circular")).strip().lower() == "ring"
                            else str(raw.get("pattern", "circular")).strip().lower()
                        ),
                        pause_s=int(raw.get("pause_s", 20)),
                        flow_ml_s=float(raw.get("flow_ml_s", 3.5)),
                        label=str(raw["label"]) if raw.get("label") is not None else None,
                    )
                )
            except KeyError as exc:
                raise TeaRecipeError(f"tea pour {index} is missing key {exc.args[0]!r}") from exc
            except (TypeError, ValueError) as exc:
                raise TeaRecipeError(f"tea pour {index} contains a non-numeric value") from exc

        try:
            recipe = cls(
                name=str(data.get("name", "Unnamed tea")),
                kind="tea",
                leaf_g=float(data["leaf_g"]),
                output_ml_per_steep=int(data["output_ml_per_steep"]),
                pours=tuple(pours),
            )
        except (TypeError, ValueError) as exc:
            raise TeaRecipeError("leaf_g and output_ml_per_steep must be numeric") from exc
        recipe.validate()
        return recipe

    @classmethod
    def from_yaml(cls, path: str | Path) -> "TeaRecipe":
        resolved = Path(path).expanduser().resolve(strict=True)
        if resolved.suffix.lower() not in {".yaml", ".yml", ".json"}:
            raise TeaRecipeError("tea recipe must be a local .yaml, .yml, or .json file")
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def validate(self) -> None:
        errors: list[str] = []
        if not 3.0 <= float(self.leaf_g) <= 5.0:
            errors.append("leaf_g must be 3-5 g for the Omni Tea Brewer")
        if not 80 <= int(self.output_ml_per_steep) <= 160:
            errors.append("output_ml_per_steep must be 80-160 ml")
        if not 1 <= len(self.pours) <= 4:
            errors.append("tea recipe must have 1-4 steep stages")

        total = 0
        for index, pour in enumerate(self.pours, start=1):
            if not 40 <= int(pour.ml) <= 100:
                errors.append(f"tea pour {index} volume must be 40-100 ml")
            if not 70 <= int(pour.temp_c) <= 99:
                errors.append(f"tea pour {index} temperature must be 70-99 C")
            if pour.pattern not in PATTERNS:
                errors.append(
                    f"tea pour {index} pattern must be one of {sorted(PATTERNS)}"
                )
            if not 1 <= int(pour.pause_s) <= 120:
                errors.append(f"tea pour {index} pause must be 1-120 s")
            flow10 = round(float(pour.flow_ml_s) * 10)
            if flow10 not in range(30, 36) or abs(flow10 / 10 - pour.flow_ml_s) > 1e-6:
                errors.append(f"tea pour {index} flow must be 3.0-3.5 ml/s in 0.1 steps")
            total += int(pour.ml)
        if total > 360:
            errors.append("total programmed tea water must not exceed 360 ml")
        if errors:
            raise TeaRecipeError("; ".join(errors))

    def to_protocol_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "pours": [pour.to_protocol_dict() for pour in self.pours],
            "cup_min_mm": 40.0,
            "cup_max_mm": 80.0,
            "grinder_size": 50,
            "grand_water": 45.0,
            "rpm": 120,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "tea",
            "leaf_g": self.leaf_g,
            "steeps": len(self.pours),
            "programmed_water_ml": sum(pour.ml for pour in self.pours),
            "output_ml_per_steep": self.output_ml_per_steep,
            "temperatures_c": [pour.temp_c for pour in self.pours],
            "pauses_s": [pour.pause_s for pour in self.pours],
        }

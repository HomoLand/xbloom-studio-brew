"""Recipe model, YAML loading, and validation for xBloom Studio brews.

A recipe describes a pour-over: the dose, grind, and an ordered list of pours.
It is validated independently of any hardware so
that mistakes are caught before anything is sent over BLE.

YAML schema
-----------
.. code-block:: yaml

    name: Example
    dose_g: 16
    grind: 62
    ratio: 15                    # optional; if given, Σpours must equal dose_g*ratio
    bypass_ml: 40                # optional machine bypass, 5-100 ml
    bypass_temp_c: RT            # RT, BP, or a numeric brew temperature
    # optional brew-level metadata (informational — NOT sent to the machine):
    dripper: Omni
    kind: custom
    water_ml: 240
    time: "~2:00"
    note: strawberry-forward, ground finer as it aged
    pours:
      - {label: Bloom,  ml: 35,  temp_c: 90, pattern: spiral, pause_s: 40, rpm: 100, flow_ml_s: 3.0}
      - {label: Pour 1, ml: 115, temp_c: 90, pattern: spiral, pause_s: 5,  rpm: 100, flow_ml_s: 3.0}

Patterns: ``spiral``, ``circular``, ``center`` (legacy ``ring`` is accepted as
an alias). ``vibration`` is independent and accepts ``none``, ``before``,
``after``, or ``both``. The old ``agitation`` boolean remains readable for
backward compatibility but should not be used in new recipes. Pour and bypass temperatures accept the
official ``RT`` (room-temperature pass-through) and ``BP`` (boiling-point) tokens.
The metadata fields (``dripper``, ``kind``,
``water_ml``, ``hot_water_ml``, ``ice_g``, ``time``, ``note``, and per-pour
``label``) are optional context that round-trips through YAML but never reaches
the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .protocol import (
    COFFEE_CUP_GEOMETRY_COMPAT,
    MACHINE_PATTERN_CODES,
    PATTERN_CODES,
    VIBRATION_CODES,
)

__all__ = ["Pour", "Recipe", "RecipeError", "parse_temperature_setting"]

ROOM_TEMPERATURE_C = 20
BOILING_POINT_C = 98


class RecipeError(ValueError):
    """Raised when a recipe is malformed or fails validation."""


def parse_temperature_setting(value: Any, *, field: str = "temperature") -> int:
    """Normalize an app-style recipe temperature to its Studio/J15 token.

    ``RT`` and ``BP`` are protocol mode values (20 and 98 respectively), not
    promises of active cooling or a universal physical boiling point.
    """
    if isinstance(value, str):
        token = value.strip().upper()
        if token == "RT":
            return ROOM_TEMPERATURE_C
        if token == "BP":
            return BOILING_POINT_C
        try:
            value = int(token)
        except ValueError as exc:
            raise RecipeError(f"{field} must be RT, BP, or an integer Celsius value") from exc
    if isinstance(value, bool):
        raise RecipeError(f"{field} must be RT, BP, or an integer Celsius value")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RecipeError(f"{field} must be RT, BP, or an integer Celsius value") from exc
    if isinstance(value, float) and not value.is_integer():
        raise RecipeError(f"{field} must use whole Celsius degrees")
    return parsed


def _temperature_to_yaml(value: int) -> int | str:
    if int(value) == ROOM_TEMPERATURE_C:
        return "RT"
    if int(value) == BOILING_POINT_C:
        return "BP"
    return int(value)


@dataclass
class Pour:
    """A single pour stage."""

    ml: int
    temp_c: int
    pattern: str = "spiral"
    #: Exact APK vibration timing. ``None`` means the legacy ``agitation`` field
    #: is in use; otherwise one of none/before/after/both.
    vibration: str | None = "none"
    #: Deprecated compatibility field from early Skill releases.
    agitation: bool | None = None
    pause_s: int = 0
    rpm: int = 0
    flow_ml_s: float = 3.0
    #: Optional human label for this pour (e.g. "Bloom", "Pour 1"). Informational
    #: only — never sent to the machine.
    label: str | None = None

    def to_protocol_dict(self) -> dict[str, Any]:
        """Shape expected by :func:`xbloom_ble.protocol.build_41`."""
        data: dict[str, Any] = {
            "ml": self.ml,
            "temp": self.temp_c,
            "pattern": self.pattern,
            "pause": self.pause_s,
            "rpm": self.rpm,
            "flow": self.flow_ml_s,
        }
        if self.vibration is not None:
            data["vibration"] = self.vibration
        if self.agitation is not None:
            data["agitation"] = self.agitation
        return data

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the YAML pour shape (round-trips with :meth:`Recipe.from_dict`)."""
        d: dict[str, Any] = {}
        if self.label is not None:
            d["label"] = self.label
        d.update(
            ml=int(self.ml),
            temp_c=_temperature_to_yaml(self.temp_c),
            pattern=self.pattern,
            pause_s=int(self.pause_s),
            rpm=int(self.rpm),
            flow_ml_s=float(self.flow_ml_s),
        )
        if self.vibration is not None:
            d["vibration"] = self.vibration
        elif self.agitation is not None:
            d["agitation"] = bool(self.agitation)
        return d


@dataclass
class Recipe:
    """A full xBloom Studio recipe.

    The core fields (``dose_g``/``grind``/``pours`` and optional
    ``bypass_ml``/``bypass_temp_c``) are what the machine brews. The remaining
    fields are **optional brew-level metadata** —
    informational context a UI or recipe site can render, but which is *never*
    sent to the machine and *not* range-checked against hardware limits:

    * ``dripper`` — the dripper/brewer used (e.g. "Omni").
    * ``kind`` — recipe kind / preset base (e.g. "custom", "medium-auto").
    * ``water_ml`` — total brew water (may exceed Σ pours for bypass/iced brews).
    * ``hot_water_ml`` / ``ice_g`` — iced-brew specifics (hot water over ice).
    * ``time`` — expected brew time as a display string (e.g. "~2:00").
    * ``note`` — free-text notes about the recipe.
    """

    name: str
    dose_g: int
    grind: int
    pours: list[Pour]
    ratio: float | None = None
    tail: int = 0xA0
    # Machine-executed post-brew bypass. Zero/None means disabled.
    bypass_ml: float = 0.0
    bypass_temp_c: int | None = None
    # Optional brew-level metadata (informational — never sent to the machine).
    dripper: str | None = None
    kind: str | None = None
    water_ml: int | None = None
    hot_water_ml: int | None = None
    ice_g: int | None = None
    time: str | None = None
    note: str | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recipe:
        if not isinstance(data, dict):
            raise RecipeError("recipe must be a mapping")
        try:
            raw_pours = data["pours"]
        except KeyError as exc:
            raise RecipeError("recipe is missing required key 'pours'") from exc
        if not isinstance(raw_pours, list) or not raw_pours:
            raise RecipeError("recipe 'pours' must be a non-empty list")

        pours: list[Pour] = []
        for i, rp in enumerate(raw_pours):
            if not isinstance(rp, dict):
                raise RecipeError(f"pour #{i + 1} must be a mapping")
            try:
                if "vibration" in rp and "agitation" in rp:
                    raise RecipeError(
                        f"pour #{i + 1} cannot set both vibration and legacy agitation"
                    )
                legacy_agitation = rp.get("agitation") if "agitation" in rp else None
                if legacy_agitation is not None and not isinstance(legacy_agitation, bool):
                    raise RecipeError(f"pour #{i + 1} agitation must be true or false")
                pattern = str(rp.get("pattern", "spiral")).strip().lower()
                if legacy_agitation is not None:
                    legacy_pattern = "ring" if pattern == "circular" else pattern
                    key = (legacy_pattern, bool(legacy_agitation))
                    try:
                        _, vibration_code = PATTERN_CODES[key]
                    except KeyError as exc:
                        raise RecipeError(
                            f"pour #{i + 1} legacy pattern/agitation pair {key!r} "
                            "has no captured compatibility mapping"
                        ) from exc
                    vibration = next(
                        name for name, code in VIBRATION_CODES.items()
                        if code == vibration_code
                    )
                else:
                    vibration = str(rp.get("vibration", "none")).strip().lower()
                if pattern == "ring":
                    pattern = "circular"
                pours.append(
                    Pour(
                        ml=rp["ml"],
                        temp_c=parse_temperature_setting(
                            rp["temp_c"], field=f"pour #{i + 1} temp_c"
                        ),
                        pattern=pattern,
                        vibration=vibration,
                        # Old YAML is normalised immediately to the exact four-state
                        # field so `agitation: false` can never hide a vibration byte.
                        agitation=None,
                        pause_s=rp.get("pause_s", 0),
                        rpm=rp.get("rpm", 0),
                        flow_ml_s=rp.get("flow_ml_s", 3.0),
                        label=rp.get("label"),
                    )
                )
            except KeyError as exc:
                raise RecipeError(f"pour #{i + 1} missing key {exc}") from exc

        if "stage_temps" in data:
            legacy_geometry = data["stage_temps"]
            if not isinstance(legacy_geometry, (list, tuple)) or len(legacy_geometry) != 2:
                raise RecipeError(
                    "deprecated 'stage_temps' must be the captured [110.0, 90.0] pair"
                )
            try:
                legacy_geometry_pair = tuple(float(value) for value in legacy_geometry)
            except (TypeError, ValueError) as exc:
                raise RecipeError(
                    "deprecated 'stage_temps' must be the captured [110.0, 90.0] pair"
                ) from exc
            if legacy_geometry_pair != COFFEE_CUP_GEOMETRY_COMPAT:
                raise RecipeError(
                    "deprecated 'stage_temps' is command-8104 cup geometry, not a "
                    "tunable temperature; remove it or keep exactly [110.0, 90.0]"
                )

        for key in ("dose_g", "grind"):
            if key not in data:
                raise RecipeError(f"recipe is missing required key '{key}'")

        recipe = cls(
            name=str(data.get("name", "Unnamed")),
            dose_g=data["dose_g"],
            grind=data["grind"],
            pours=pours,
            ratio=data.get("ratio"),
            tail=data.get("tail", 0xA0),
            bypass_ml=float(data.get("bypass_ml", 0.0) or 0.0),
            bypass_temp_c=(
                parse_temperature_setting(
                    data["bypass_temp_c"], field="bypass_temp_c"
                )
                if data.get("bypass_temp_c") is not None
                else None
            ),
            # Optional brew-level metadata (informational — not sent to the machine).
            dripper=data.get("dripper"),
            kind=data.get("kind"),
            water_ml=data.get("water_ml"),
            hot_water_ml=data.get("hot_water_ml"),
            ice_g=data.get("ice_g"),
            time=data.get("time"),
            note=data.get("note"),
        )
        recipe.validate()
        return recipe

    @classmethod
    def from_yaml(cls, path: str | Path) -> Recipe:
        """Load and validate a recipe from a YAML file."""
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        if data is None:
            raise RecipeError(f"recipe file {path} is empty")
        return cls.from_dict(data)

    @classmethod
    def from_yaml_text(cls, text: str, *, origin: str = "<text>") -> Recipe:
        """Load and validate a recipe from a YAML string."""
        data = yaml.safe_load(text)
        if data is None:
            raise RecipeError(f"recipe {origin} is empty")
        return cls.from_dict(data)

    @classmethod
    def from_source(cls, src: str | Path, *, timeout: float = 15.0) -> Recipe:
        """Load a recipe from a local path **or** an ``http(s)://`` URL.

        URLs are fetched with a short timeout and a 1 MB size cap; the body is
        parsed as the same YAML recipe format. Lets recipes be shared/served
        (e.g. ``xbloom brew https://…/teso-la-leona.yaml``).
        """
        s = str(src)
        if s.startswith(("http://", "https://")):
            from urllib.request import Request, urlopen

            req = Request(s, headers={"User-Agent": "xbloom-ble"})
            with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - user-supplied URL
                text = resp.read(1_000_000).decode("utf-8")
            return cls.from_yaml_text(text, origin=s)
        return cls.from_yaml(s)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Validate the recipe, raising :class:`RecipeError` on any problem."""
        errors: list[str] = []

        # dose / grind
        # dose: the xBloom app caps the dose at 18 g (firm app maximum).
        if not (1 <= int(self.dose_g) <= 18):
            errors.append(f"dose_g {self.dose_g} out of range (1–18 g; 18 g is the app maximum)")
        # grind: 1–80 — the grinder has 80 micro-steps (~18.75 µm each); a lower
        # number is finer. (xBloom Studio published spec.) A grind of 0 is the
        # special "no-grind" value: brew pre-ground, grinder off (see NO_GRIND).
        if int(self.grind) != 0 and not (1 <= int(self.grind) <= 80):
            errors.append(
                f"grind {self.grind} out of range (1–80; the grinder has 80 micro-steps — "
                f"or 0 for no-grind / pre-ground)"
            )

        # need at least a bloom + a first pour
        if len(self.pours) < 2:
            errors.append("recipe needs at least a bloom pour and a first pour (≥2 pours)")

        total_ml = 0
        for i, p in enumerate(self.pours, start=1):
            pattern = str(p.pattern).strip().lower()
            if pattern not in MACHINE_PATTERN_CODES:
                errors.append(
                    f"pour #{i}: pattern {p.pattern!r} must be one of "
                    f"{sorted(MACHINE_PATTERN_CODES)}"
                )
            if p.vibration is not None and p.agitation is not None:
                errors.append(
                    f"pour #{i}: cannot set both vibration and legacy agitation"
                )
            elif p.vibration is not None:
                if str(p.vibration).strip().lower() not in VIBRATION_CODES:
                    errors.append(
                        f"pour #{i}: vibration {p.vibration!r} must be one of "
                        f"{sorted(VIBRATION_CODES)}"
                    )
            elif ("ring" if pattern == "circular" else pattern, bool(p.agitation)) not in PATTERN_CODES:
                valid = sorted({pat for pat, _ in PATTERN_CODES})
                errors.append(
                    f"pour #{i}: legacy pattern/agitation ({p.pattern!r}, {p.agitation}) "
                    f"not in known set {valid}"
                )
            # A pour over 127 ml is auto-split by the protocol — that is fine,
            # not an error. ml just needs to be ≥1 and fit a sane upper bound.
            if not (1 <= int(p.ml) <= 4000):
                errors.append(f"pour #{i}: ml {p.ml} out of range (1–4000)")
            # App recipe editor: RT sentinel, 40–95 °C, or BP sentinel.
            if int(p.temp_c) not in {ROOM_TEMPERATURE_C, BOILING_POINT_C} and not (
                40 <= int(p.temp_c) <= 95
            ):
                errors.append(
                    f"pour #{i}: temp_c {p.temp_c} out of range "
                    "(RT, 40–95°C, or BP)"
                )
            # rpm: agitation speed, 60–120 in 10-RPM steps — EXCEPT a `center` pour
            # has no agitation, where rpm must be 0. (xBloom Studio published spec.)
            if p.pattern == "center":
                if int(p.rpm) != 0 and not (60 <= int(p.rpm) <= 120):
                    errors.append(
                        f"pour #{i}: rpm {p.rpm} out of range (0 for a center pour, else 60–120)"
                    )
            else:
                if not (60 <= int(p.rpm) <= 120):
                    errors.append(f"pour #{i}: rpm {p.rpm} out of range (60–120)")
            # flow: 3.0–3.5 ml/s in 0.1 steps (xBloom Studio published spec).
            if not (3.0 <= float(p.flow_ml_s) <= 3.5):
                errors.append(f"pour #{i}: flow_ml_s {p.flow_ml_s} out of range (3.0–3.5)")
            # pause: the wire byte is (256 − seconds), so it can hold 0–255, but
            # the on-machine countdown caps near 99 s — that is the practical
            # range. We accept the full byte range here.
            if not (0 <= int(p.pause_s) <= 255):
                errors.append(f"pour #{i}: pause_s {p.pause_s} out of range (0–255)")
            total_ml += int(p.ml)

        # ratio check (only if a ratio is supplied)
        if self.ratio is not None:
            expected = round(float(self.dose_g) * float(self.ratio))
            if total_ml != expected:
                errors.append(
                    f"Σpours = {total_ml} ml but dose_g*ratio = "
                    f"{self.dose_g}*{self.ratio} = {expected} ml"
                )

        if self.bypass_ml:
            if not 5 <= float(self.bypass_ml) <= 100:
                errors.append("bypass_ml must be 5-100 ml when enabled")
            elif not float(self.bypass_ml).is_integer():
                errors.append("bypass_ml must use whole millilitres")
            if self.bypass_temp_c is None:
                errors.append("bypass_temp_c is required when bypass_ml is enabled")
            elif int(self.bypass_temp_c) not in {
                ROOM_TEMPERATURE_C,
                BOILING_POINT_C,
            } and not (40 <= int(self.bypass_temp_c) <= 95):
                errors.append("bypass_temp_c must be RT, 40-95 C, or BP")
        elif self.bypass_temp_c is not None:
            errors.append("bypass_temp_c requires bypass_ml")

        # Optional metadata: only sanity-check the numeric ones (not hardware
        # limits — these never reach the machine). Deliberately lenient so
        # informational context can't break a valid, brewable recipe.
        for label, v in (("water_ml", self.water_ml), ("hot_water_ml", self.hot_water_ml),
                         ("ice_g", self.ice_g)):
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                errors.append(f"{label} must be a non-negative number, got {v!r}")

        if errors:
            raise RecipeError("; ".join(errors))

    # ------------------------------------------------------------------
    # Protocol bridge
    # ------------------------------------------------------------------
    @property
    def no_grind(self) -> bool:
        """True if this recipe brews **pre-ground** (grinder off) — i.e. ``grind == 0``.

        The machine is asked to skip the grinder (wire byte ``0xFE``); its stored
        grind size is left untouched.
        """
        return int(self.grind) == 0

    @property
    def total_water_ml(self) -> int:
        return sum(int(p.ml) for p in self.pours)

    @property
    def total_machine_water_ml(self) -> float:
        """Extraction pours plus optional post-brew bypass water."""
        return float(self.total_water_ml) + float(self.bypass_ml or 0.0)

    @property
    def effective_ratio(self) -> float:
        """Brew ratio (water : coffee). Uses the explicit ``ratio`` if given,
        else derives it from ``Σ pour ml / dose_g`` (one decimal). Used by the
        cloud mapping (``grandWater``)."""
        if self.ratio is not None:
            return float(self.ratio)
        return round(self.total_water_ml / float(self.dose_g), 1) if self.dose_g else 0.0

    def to_protocol_dict(self) -> dict[str, Any]:
        """Shape consumed by :func:`xbloom_ble.protocol.build_load_frames`.

        Note: ``tail`` (the pours-frame ratio byte) is intentionally omitted — the
        protocol layer derives it from Σ(pour ml) / dose, so it always matches the
        recipe. The machine rejects a load whose ratio byte is inconsistent.
        """
        return {
            "dose": int(self.dose_g),
            "grind": int(self.grind),
            "cup_geometry_compat": COFFEE_CUP_GEOMETRY_COMPAT,
            "bypass_ml": float(self.bypass_ml or 0.0),
            "bypass_temp_c": float(self.bypass_temp_c or 0.0),
            "pours": [p.to_protocol_dict() for p in self.pours],
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the YAML recipe shape (round-trips with :meth:`from_dict`).

        Emits the core fields plus any optional brew-level metadata that is set
        (omitting ``None``), so a recipe read from YAML and written back is stable.
        """
        d: dict[str, Any] = {"name": self.name, "dose_g": int(self.dose_g),
                             "grind": int(self.grind)}
        if self.ratio is not None:
            d["ratio"] = self.ratio
        if self.bypass_ml:
            d["bypass_ml"] = float(self.bypass_ml)
            d["bypass_temp_c"] = _temperature_to_yaml(self.bypass_temp_c)
        # optional metadata, in a stable, readable order (only when present)
        for key in ("kind", "dripper", "water_ml", "hot_water_ml", "ice_g", "time", "note"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        d["pours"] = [p.to_dict() for p in self.pours]
        return d

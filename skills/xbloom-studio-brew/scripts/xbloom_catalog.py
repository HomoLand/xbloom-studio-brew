"""Private xBloom recipe-catalog import, sync, query, and export support.

The Android app does not bundle its full recipe library in the APK. It fetches
account/region-visible records and caches them in MMKV. This module normalises
authorised JSON exports or bounded own-account cloud responses into a user-local
catalog, and can preview or explicitly add one guarded recipe. It never stores
request credentials, login sessions, or raw account blobs.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import yaml

from xbloom_ble.recipe import Recipe
from xbloom_ble.tea import TeaRecipe
from xbloom_safety import strict_validate, validate_slot_compatible


SCHEMA_VERSION = 1
CATALOG_PATH_ENV = "XBLOOM_CATALOG_PATH"
CLOUD_CONFIG_ENV = "XBLOOM_CLOUD_CONFIG"
MAX_IMPORT_BYTES = 25 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
ACCOUNT_EMAIL_ENV = "XBLOOM_ACCOUNT_EMAIL"
ACCOUNT_PASSWORD_ENV = "XBLOOM_ACCOUNT_PASSWORD"
CLOUD_WRITE_CONFIRM_SENTINEL = "own-account-cloud-recipe"

# Public (not secret) key used by BaseTransfer in xBloom Android 2.2.2 for the
# legacy .thtml/.tuhtml account and recipe APIs. The APK also contains a second
# RSAEncrypt class for a different request stack; it is not interchangeable.
# BaseTransfer encrypts each JSON form with PKCS#1 v1.5, concatenates 1024-bit
# blocks, then base64 encodes the result before POSTing it as a JSON string.
APP_RSA_PUBLIC_KEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC4LF40GZ72SdhMyl765K/i4nY5"
    "CPcHz2Q1IKWKZ9S79xmK7G8pUhbVf4EZLvnNF1+9IvOFQUKV5Z7ZNNviqSpnql9t"
    "AT+8+J/He0R7pcirvVSxgdr2i9V/C/gmqAEZ5qVTzRnd3uWdFoKzPdEBxP0IporJ1"
    "VBbCv90yBSOhVxO+QIDAQAB"
)

BASE_URLS = {
    "international": "https://client-api.xbloom.com/",
    "china": "https://clientcn-api.xbloomcoffee.cn/",
}
LOGIN_ENDPOINT = "tMemberLogin.thtml"
APP_VERSION = "2.2.2"
LOGIN_INTERFACE_VERSION = 20240918
CATALOG_INTERFACE_VERSION = 19700101
APP_SKEY = "testskey"
DEFAULT_CLIENT_TYPE = 7
ENDPOINTS = {
    "coffee": "tHostRecipe.thtml",
    "tea": "tuTeaRecipe.tuhtml",
    # This Studio/J15 endpoint returns both coffee and tea recipes created by
    # the signed-in member. The older tuMyRecipeCreated endpoint omits tea.
    "created": "tuMyTeaRecipeCreated.tuhtml",
    "product": "tuMyRecipeProduct.tuhtml",
    "shared": "tuMyRecipeShared.tuhtml",
    "easy": "tuEasyModeList.tuhtml",
    "easy-default": "tuEasyModeInitList.tuhtml",
}
DEFAULT_ACCOUNT_TARGETS = ("coffee", "tea", "created", "product", "shared")
RECIPE_ADD_ENDPOINT = "tuRecipeAdd.tuhtml"
RECIPE_WRITE_INTERFACE_VERSION = 20240918
DEFAULT_RECIPE_COLOR = "#ADBDDB"
APP_PLACE_LABELS = {
    1: "hot",
    2: "curated",
    3: "tea",
    4: "created",
    5: "shared",
    6: "xbloom",
}
CUP_TYPE_LABELS = {1: "xpod", 2: "omni", 3: "other", 4: "tea"}
APP_PATTERN_LABELS = {1: "center", 2: "spiral", 3: "circular"}
APP_PATTERN_VALUES = {value: key for key, value in APP_PATTERN_LABELS.items()}
CODE_PATTERN_LABELS = {0: "center", 1: "circular", 2: "spiral"}
VIBRATION_LABELS = {0: "none", 1: "before", 2: "after", 3: "both"}
KNOWN_CONTAINERS = {
    "list",
    "recipes",
    "recipeList",
    "easyModeDetailVoList",
    "DiskRecipeList",
    "DiskEasyModeDeviceList",
    "data",
    "payload",
    "response",
    "result",
    "value",
}


class CatalogError(RuntimeError):
    """Raised for malformed imports, unsafe exports, or cloud-sync failures."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_catalog_path(state_dir: Path) -> Path:
    configured = os.environ.get(CATALOG_PATH_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path(state_dir) / "catalog" / "catalog.json"


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CatalogError(f"expected integer, got {value!r}")
    if isinstance(value, int):
        return value
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CatalogError(f"expected integer, got {value!r}") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise CatalogError(f"expected whole number, got {value!r}")
    return int(number)


def _required_int(value: Any, field: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise CatalogError(f"missing {field}")
    return parsed


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise CatalogError(f"expected number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CatalogError(f"expected number, got {value!r}") from exc
    if not math.isfinite(number):
        raise CatalogError(f"expected finite number, got {value!r}")
    return number


def _normalise_app_places(value: Any) -> list[int]:
    value = _jsonish(value)
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    out: list[int] = []
    for item in value:
        try:
            parsed = _optional_int(item)
        except (CatalogError, TypeError, ValueError):
            continue
        if parsed is not None and parsed not in out:
            out.append(parsed)
    return sorted(out)


def _looks_like_recipe(value: Mapping[str, Any]) -> bool:
    has_pours = any(key in value for key in ("pourList", "pours", "pourDataJSONStr"))
    has_identity = any(
        key in value
        for key in ("tableId", "theName", "name", "dose", "dose_g", "leaf_g")
    )
    return has_pours and has_identity


def _candidate_records(
    value: Any,
    *,
    context: Mapping[str, Any] | None = None,
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield recipe objects from API envelopes and decoded MMKV JSON dumps."""

    value = _jsonish(value)
    ctx = dict(context or {})
    if isinstance(value, list):
        for item in value:
            yield from _candidate_records(item, context=ctx)
        return
    if not isinstance(value, dict):
        return

    snapshot = _jsonish(value.get("recipeSnapshotVo"))
    if isinstance(snapshot, dict):
        nested = dict(ctx)
        position = _first(value, "position", "slot")
        if position is not None:
            position_text = str(position).strip().upper()
            if position_text in {"A", "B", "C"}:
                scale = True
                try:
                    scale_value = _optional_int(value.get("scale"))
                    if scale_value not in (None, 1, 2):
                        nested.setdefault("warnings", []).append(
                            f"Easy slot {position_text} has unknown scale value; defaulted to on"
                        )
                    else:
                        scale = scale_value != 2
                except (CatalogError, TypeError, ValueError):
                    nested.setdefault("warnings", []).append(
                        f"Easy slot {position_text} has invalid scale value; defaulted to on"
                    )
                nested["slot"] = {"position": position_text, "scale": scale}
            else:
                nested.setdefault("warnings", []).append(
                    f"ignored unknown Easy slot position {position_text!r}"
                )
        yield from _candidate_records(snapshot, context=nested)
        return

    if _looks_like_recipe(value):
        yield dict(value), ctx
        return

    visited: set[str] = set()
    for key in KNOWN_CONTAINERS:
        if key in value:
            visited.add(key)
            nested = dict(ctx)
            if key == "easyModeDetailVoList":
                nested["easy_mode"] = True
            yield from _candidate_records(value[key], context=nested)

    # Decoded MMKV maps and response wrappers sometimes use device IDs or recipe
    # IDs as keys. Recurse only into structured/JSON-like values; scalar account
    # fields are deliberately ignored and never persisted.
    for key, item in value.items():
        if key in visited or key == "recipeSnapshotVo":
            continue
        parsed = _jsonish(item)
        if isinstance(parsed, (dict, list)):
            yield from _candidate_records(parsed, context=ctx)


def _normalise_pattern(pour: Mapping[str, Any]) -> str:
    if "setting_pattern_array" in pour:
        raw = pour["setting_pattern_array"]
        if isinstance(raw, str) and not raw.strip().isdigit():
            return raw.strip().lower()
        parsed = _required_int(raw, "pour pattern")
        if parsed not in CODE_PATTERN_LABELS:
            raise CatalogError(f"unknown encoded pour pattern {parsed}")
        return CODE_PATTERN_LABELS[parsed]
    raw = _first(pour, "pattern", default="circular")
    if isinstance(raw, str) and not raw.strip().lstrip("-").isdigit():
        text = raw.strip().lower()
        return "circular" if text == "ring" else text
    parsed = _required_int(raw, "pour pattern")
    if parsed not in APP_PATTERN_LABELS:
        raise CatalogError(f"unknown app pour pattern {parsed}")
    return APP_PATTERN_LABELS[parsed]


def _normalise_vibration(pour: Mapping[str, Any]) -> str:
    direct = _first(pour, "vibration", "vibration_pattern")
    if direct is not None:
        if isinstance(direct, str) and not direct.strip().isdigit():
            return direct.strip().lower()
        parsed = _required_int(direct, "vibration pattern")
        if parsed not in VIBRATION_LABELS:
            raise CatalogError(f"unknown vibration pattern {parsed}")
        return VIBRATION_LABELS[parsed]
    before = _optional_int(_first(pour, "isEnableVibrationBefore", default=2)) == 1
    after = _optional_int(_first(pour, "isEnableVibrationAfter", default=2)) == 1
    return VIBRATION_LABELS[(1 if before else 0) | (2 if after else 0)]


def _normalise_pours(
    raw: Any,
    *,
    top_rpm: int | None,
    tea: bool,
    warnings: list[str],
) -> list[dict[str, Any]]:
    raw = _jsonish(raw)
    if isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list) or not raw:
        raise CatalogError("recipe has no pour list")
    pours: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        item = _jsonish(item)
        if not isinstance(item, dict):
            raise CatalogError(f"pour {index} is not an object")
        stage = item
        substeps = _jsonish(item.get("subStep"))
        if isinstance(substeps, list) and substeps and isinstance(substeps[0], dict):
            stage = {**item, **substeps[0]}
        pattern = _normalise_pattern(stage)
        volume = _first(stage, "volume", "ml")
        if volume is None and "setting_pour_over_array" in stage:
            volume = stage["setting_pour_over_array"]
        pause = _first(item, "pausing", "pause_s", "pause", default=20 if tea else 5)
        if "setting_pour_over_array" in item and substeps:
            pause = item["setting_pour_over_array"]
        temperature = _first(item, "temperature", "temp_c", "temp", "setting_bloom_temp")
        flow = _optional_float(_first(item, "flowRate", "flow_ml_s", "flow", default=3.5 if tea else 3.0))
        if flow is None:
            flow = 3.5 if tea else 3.0
        if flow > 10:
            flow /= 10.0
        pour: dict[str, Any] = {
            "ml": _required_int(volume, f"pour {index} volume"),
            "temp_c": _required_int(temperature, f"pour {index} temperature"),
            "pattern": pattern,
            "pause_s": _required_int(pause, f"pour {index} pause"),
            "flow_ml_s": flow,
        }
        label = _first(item, "theName", "label")
        if label:
            pour["label"] = str(label)
        if not tea:
            pour["vibration"] = _normalise_vibration(stage)
            if pattern == "center":
                pour["rpm"] = 0
            else:
                rpm = _optional_int(_first(item, "rpm", default=top_rpm))
                if rpm is None:
                    rpm = 120
                    if "missing RPM defaulted to 120" not in warnings:
                        warnings.append("missing RPM defaulted to 120")
                pour["rpm"] = rpm
        pours.append(pour)
    return pours


def _normalise_coffee(
    raw: Mapping[str, Any],
    *,
    cup_type: int | None,
    warnings: list[str],
) -> dict[str, Any]:
    if "dose_g" in raw and "pours" in raw:
        parsed = Recipe.from_dict(dict(raw))
        strict_validate(parsed)
        return parsed.to_dict()

    dose_value = _optional_float(_first(raw, "dose", "dose_g"))
    if dose_value is None:
        raise CatalogError("missing coffee dose")
    dose: int | float = int(dose_value) if dose_value.is_integer() else dose_value
    set_grinder = _optional_int(_first(raw, "isSetGrinderSize", default=1))
    if set_grinder != 1:
        grind: int | float = 0
    else:
        grind_value = _optional_float(_first(raw, "grinderSize", "grind"))
        if grind_value is None:
            raise CatalogError("missing grinder size")
        grind = int(grind_value) if grind_value.is_integer() else grind_value
    top_rpm = _optional_int(_first(raw, "rpm"))
    pours = _normalise_pours(
        _first(raw, "pourList", "pours", "pourDataJSONStr"),
        top_rpm=top_rpm,
        tea=False,
        warnings=warnings,
    )
    recipe: dict[str, Any] = {
        "name": str(_first(raw, "theName", "name", default="Unnamed xBloom recipe")),
        "dose_g": dose,
        "grind": grind,
        "kind": "hot",
        "dripper": "Omni",
    }
    ratio = _optional_float(_first(raw, "grandWater", "ratio"))
    if ratio is not None:
        pour_total = sum(int(pour["ml"]) for pour in pours)
        rounded_ratio_total = round(float(dose) * ratio)
        if pour_total != rounded_ratio_total and abs(pour_total - rounded_ratio_total) <= 1:
            warnings.append(
                "app grandWater differs from the integer pour total by 1 ml; "
                "ratio will be derived from pours"
            )
        else:
            recipe["ratio"] = ratio
    else:
        warnings.append("missing grandWater ratio; ratio will be derived from pours and dose")

    bypass_volume = _optional_float(_first(raw, "bypassVolume", "bypass_ml", default=0)) or 0.0
    has_bypass_flag = "isEnableBypassWater" in raw
    bypass_enabled = (
        _optional_int(raw.get("isEnableBypassWater")) == 1
        if has_bypass_flag
        else bool(bypass_volume)
    )
    if bypass_enabled:
        recipe["bypass_ml"] = bypass_volume
        bypass_temp = _first(raw, "bypassTemp", "bypass_temp_c")
        if bypass_temp is not None:
            recipe["bypass_temp_c"] = _required_int(bypass_temp, "bypass temperature")
    elif bypass_volume:
        warnings.append("disabled app bypass values were ignored")
    total = sum(int(pour["ml"]) for pour in pours) + int(
        bypass_volume if bypass_enabled else 0
    )
    recipe["water_ml"] = total
    recipe["pours"] = pours
    if cup_type == 1:
        # Keep the source geometry explicit. The catalog entry will be marked
        # reference-only until an Agent performs the Skill's xPod→Omni adaptation.
        recipe["dripper"] = "xPod"
    return recipe


def _normalise_tea(raw: Mapping[str, Any], *, warnings: list[str]) -> dict[str, Any]:
    if str(raw.get("kind", "")).strip().lower() == "tea" and "leaf_g" in raw:
        recipe = TeaRecipe.from_dict(dict(raw))
        return {
            "name": recipe.name,
            "kind": "tea",
            "leaf_g": recipe.leaf_g,
            "output_ml_per_steep": recipe.output_ml_per_steep,
            "pours": [
                {
                    **({"label": pour.label} if pour.label else {}),
                    "ml": pour.ml,
                    "temp_c": pour.temp_c,
                    "pattern": pour.pattern,
                    "pause_s": pour.pause_s,
                    "flow_ml_s": pour.flow_ml_s,
                }
                for pour in recipe.pours
            ],
        }
    leaf = _optional_float(_first(raw, "dose", "leaf_g"))
    if leaf is None:
        leaf = 4.0
        warnings.append("missing tea dose defaulted to 4 g")
    if leaf <= 0:
        raise CatalogError("tea dose must be positive")
    output = _optional_int(
        _first(raw, "outputMlPerSteep", "output_ml_per_steep")
    )
    if output is None:
        output = 120
        warnings.append(
            "output_ml_per_steep inferred as ~120 ml; the app treats this as finished "
            "siphon output metadata, not a programmed 120 ml pour"
        )
    pours = _normalise_pours(
        _first(raw, "pourList", "pours", "pourDataJSONStr"),
        top_rpm=None,
        tea=True,
        warnings=warnings,
    )
    source_ratio = _optional_float(_first(raw, "grandWater", "ratio"))
    derived_ratio = sum(int(pour["ml"]) for pour in pours) / float(leaf)
    if source_ratio is not None and abs(source_ratio - derived_ratio) > 0.05:
        warnings.append(
            "tea grandWater does not match programmed water / leaf dose; the guarded "
            "protocol will derive the ratio from the stages"
        )
    return {
        "name": str(_first(raw, "theName", "name", default="Unnamed xBloom tea")),
        "kind": "tea",
        "leaf_g": leaf,
        "output_ml_per_steep": output,
        "pours": pours,
    }


def _origin(
    *,
    raw: Mapping[str, Any],
    context: Mapping[str, Any],
    app_places: list[int],
    cup_type: int | None,
    endpoint: str | None,
) -> str:
    if context.get("slot") or context.get("easy_mode"):
        return "easy-mode"
    if endpoint == ENDPOINTS["created"]:
        return "user-created"
    if endpoint == ENDPOINTS["shared"]:
        return "shared"
    if cup_type == 1 or any(_first(raw, key) for key in ("podsId", "podsXid", "podsName")):
        return "xpod"
    if 6 in app_places:
        return "xbloom"
    if 4 in app_places:
        return "user-created"
    if 5 in app_places:
        return "shared"
    if endpoint in {ENDPOINTS["coffee"], ENDPOINTS["tea"]}:
        return "xbloom-hosted"
    if 2 in app_places:
        return "curated"
    return "app-catalog"


def _source_record(
    *,
    source_type: str,
    source_file: str | None,
    endpoint: str | None,
    region: str | None,
    imported_at: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {"type": source_type, "imported_at": imported_at}
    if source_file:
        record["file"] = Path(source_file).name
    if endpoint:
        record["endpoint"] = endpoint
    if region:
        record["region"] = region
    return record


def normalise_entry(
    raw: Mapping[str, Any],
    *,
    context: Mapping[str, Any] | None = None,
    source_type: str = "app-json",
    source_file: str | None = None,
    endpoint: str | None = None,
    region: str | None = None,
    kind_hint: str = "auto",
    imported_at: str | None = None,
) -> dict[str, Any]:
    context = dict(context or {})
    imported_at = imported_at or utc_now()
    context_warnings = context.get("warnings")
    warnings: list[str] = (
        [str(item) for item in context_warnings]
        if isinstance(context_warnings, list)
        else []
    )
    app_places = _normalise_app_places(_first(raw, "appPlace", "app_places"))
    cup_type = _optional_int(_first(raw, "cupType", "cup_type"))
    adapted_model = _optional_int(_first(raw, "adaptedModel", "adapted_model"))
    hinted = str(kind_hint).strip().lower()
    is_tea = (
        hinted == "tea"
        or str(raw.get("kind", "")).strip().lower() == "tea"
        or cup_type == 4
        or 3 in app_places
    )
    recipe = (
        _normalise_tea(raw, warnings=warnings)
        if is_tea
        else _normalise_coffee(raw, cup_type=cup_type, warnings=warnings)
    )
    kind = "tea" if is_tea else "coffee"
    table_id = _optional_int(_first(raw, "tableId", "table_id", "recipeId"))
    stable_material = json.dumps(recipe, ensure_ascii=False, sort_keys=True).encode("utf-8")
    identifier = f"xbloom:{table_id}" if table_id is not None else (
        "local:" + hashlib.sha256(stable_material).hexdigest()[:16]
    )
    origin = _origin(
        raw=raw,
        context=context,
        app_places=app_places,
        cup_type=cup_type,
        endpoint=endpoint,
    )

    validation_errors: list[str] = []
    executable = True
    try:
        if kind == "tea":
            TeaRecipe.from_dict(recipe)
        else:
            parsed = Recipe.from_dict(recipe)
            strict_validate(parsed)
    except Exception as exc:
        executable = False
        validation_errors.append(str(exc))
    if kind == "tea":
        has_tea_bypass_flag = "isEnableBypassWater" in raw
        tea_bypass_volume = _optional_float(
            _first(raw, "bypassVolume", "bypass_ml", default=0)
        ) or 0.0
        tea_bypass_enabled = (
            _optional_int(raw.get("isEnableBypassWater")) == 1
            if has_tea_bypass_flag
            else bool(tea_bypass_volume)
        )
        if tea_bypass_enabled:
            executable = False
            validation_errors.append(
                "tea record declares bypass that the guarded Omni Tea Brewer schema cannot represent"
            )
        elif tea_bypass_volume:
            warnings.append("disabled app bypass values were ignored")
    elif "isEnableBypassWater" in raw:
        coffee_bypass_enabled = _optional_int(raw.get("isEnableBypassWater")) == 1
        coffee_bypass_volume = _optional_float(
            _first(raw, "bypassVolume", "bypass_ml", default=0)
        ) or 0.0
        if coffee_bypass_enabled and not coffee_bypass_volume:
            executable = False
            validation_errors.append(
                "coffee bypass is enabled but has no positive bypass volume"
            )
    if origin == "xpod":
        executable = False
        validation_errors.append(
            "xPod-native recipe is reference-only until explicitly adapted for loose beans and Omni"
        )
    if kind == "coffee" and cup_type == 3:
        executable = False
        validation_errors.append(
            "other-dripper recipe is reference-only until explicitly adapted for Omni"
        )
    if kind == "coffee" and cup_type == 0:
        executable = False
        validation_errors.append(
            "recipe has unknown app cup geometry and must be adapted for Omni"
        )
    if adapted_model not in (None, 1):
        executable = False
        validation_errors.append(
            f"recipe targets adaptedModel={adapted_model}, not Studio/J15"
        )

    slot_compatible = False
    slot_reason: str | None = None
    if kind == "coffee" and executable:
        try:
            validate_slot_compatible(Recipe.from_dict(recipe))
            slot_compatible = True
        except Exception as exc:
            slot_reason = str(exc)
    elif kind == "tea":
        slot_reason = "tea uses the dedicated Omni Tea Brewer path, not A/B/C"
    elif validation_errors:
        slot_reason = validation_errors[0]

    entry: dict[str, Any] = {
        "id": identifier,
        "table_id": table_id,
        "name": str(recipe["name"]),
        "kind": kind,
        "origin": origin,
        "app_places": app_places,
        "app_place_labels": [APP_PLACE_LABELS.get(item, str(item)) for item in app_places],
        "cup_type": CUP_TYPE_LABELS.get(cup_type, str(cup_type) if cup_type is not None else None),
        "adapted_model": adapted_model,
        "executable": executable,
        "slot_compatible": slot_compatible,
        "slot_incompatibility": slot_reason,
        "redistribution": "unknown",
        "warnings": sorted(set(warnings)),
        "validation_errors": list(dict.fromkeys(validation_errors)),
        "recipe": recipe,
        "slots": [],
        "sources": [
            _source_record(
                source_type=source_type,
                source_file=source_file,
                endpoint=endpoint,
                region=region,
                imported_at=imported_at,
            )
        ],
        "first_seen_at": imported_at,
        "last_seen_at": imported_at,
    }
    slot = context.get("slot")
    if isinstance(slot, Mapping):
        entry["slots"] = [
            {"position": str(slot.get("position", "")).upper(), "scale": bool(slot.get("scale", True))}
        ]
    share_link = _first(raw, "shareRecipeLink", "share_link")
    if isinstance(share_link, str) and share_link.startswith(("http://", "https://")):
        entry["share_link"] = share_link
    author = _first(raw, "resourceMemberName", "author")
    if author:
        entry["author"] = str(author)
    pods_name = _first(raw, "podsName", "pods_name")
    pods_xid = _first(raw, "podsXid", "pods_xid")
    if pods_name or pods_xid:
        entry["xpod"] = {
            **({"name": str(pods_name)} if pods_name else {}),
            **({"xid": str(pods_xid)} if pods_xid else {}),
        }
    return entry


def empty_catalog() -> dict[str, Any]:
    now = utc_now()
    return {"schema_version": SCHEMA_VERSION, "created_at": now, "updated_at": now, "entries": []}


def load_catalog(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return empty_catalog()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"catalog at {resolved} is unreadable") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(f"unsupported catalog schema at {resolved}")
    entries = data.get("entries")
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise CatalogError(f"catalog entries at {resolved} are invalid")
    return data


def save_catalog(catalog: Mapping[str, Any], path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    data = deepcopy(dict(catalog))
    data["schema_version"] = SCHEMA_VERSION
    data["updated_at"] = utc_now()
    temp = resolved.with_suffix(resolved.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(resolved)
    return resolved


def _merge_entry(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(incoming))
    merged["first_seen_at"] = existing.get("first_seen_at", incoming.get("first_seen_at"))
    old_sources = list(existing.get("sources") or [])
    new_sources = list(incoming.get("sources") or [])
    source_map: dict[tuple[Any, Any, Any, Any], dict[str, Any]] = {}
    for source in [*old_sources, *new_sources]:
        if not isinstance(source, dict):
            continue
        key = (
            source.get("type"),
            source.get("endpoint"),
            source.get("file"),
            source.get("region"),
        )
        source_map[key] = source
    merged["sources"] = sorted(
        source_map.values(),
        key=lambda item: (str(item.get("type")), str(item.get("endpoint")), str(item.get("file"))),
    )
    slots: dict[str, dict[str, Any]] = {}
    for slot in [*(existing.get("slots") or []), *(incoming.get("slots") or [])]:
        if isinstance(slot, dict) and slot.get("position"):
            slots[str(slot["position"]).upper()] = slot
    merged["slots"] = [slots[key] for key in sorted(slots)]
    return merged


def import_payload(
    catalog: dict[str, Any],
    payload: Any,
    *,
    source_type: str = "app-json",
    source_file: str | None = None,
    endpoint: str | None = None,
    region: str | None = None,
    kind_hint: str = "auto",
    imported_at: str | None = None,
) -> dict[str, Any]:
    imported_at = imported_at or utc_now()
    candidates = list(_candidate_records(payload, context={}))
    if not candidates:
        raise CatalogError("no xBloom recipe records were found in the supplied JSON")
    by_id = {str(entry.get("id")): entry for entry in catalog.get("entries", [])}
    added = 0
    updated = 0
    rejected: list[dict[str, Any]] = []
    for index, (raw, context) in enumerate(candidates, start=1):
        try:
            entry = normalise_entry(
                raw,
                context=context,
                source_type=source_type,
                source_file=source_file,
                endpoint=endpoint,
                region=region,
                kind_hint=kind_hint,
                imported_at=imported_at,
            )
        except (CatalogError, TypeError, ValueError) as exc:
            rejected.append(
                {
                    "index": index,
                    "name": str(_first(raw, "theName", "name", default="unknown")),
                    "error": str(exc),
                }
            )
            continue
        identifier = entry["id"]
        if identifier in by_id:
            by_id[identifier] = _merge_entry(by_id[identifier], entry)
            updated += 1
        else:
            by_id[identifier] = entry
            added += 1
    if not added and not updated:
        first = rejected[0]["error"] if rejected else "no normalisable records"
        raise CatalogError(f"no recipe could be imported: {first}")
    catalog["entries"] = sorted(
        by_id.values(),
        key=lambda item: (item["kind"], item["name"].casefold(), item["id"]),
    )
    catalog["updated_at"] = imported_at
    return {
        "candidates": len(candidates),
        "added": added,
        "updated": updated,
        "rejected": len(rejected),
        "rejections": rejected[:20],
        "total": len(catalog["entries"]),
    }


def import_json_file(
    catalog: dict[str, Any],
    path: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=True)
    if resolved.stat().st_size > MAX_IMPORT_BYTES:
        raise CatalogError(f"catalog import exceeds {MAX_IMPORT_BYTES} bytes")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{resolved} is not a valid UTF-8 JSON export") from exc
    return import_payload(catalog, payload, source_file=resolved.name, **kwargs)


def list_entries(
    catalog: Mapping[str, Any],
    *,
    kind: str = "all",
    origin: str | None = None,
    query: str | None = None,
    executable_only: bool = False,
    slot_compatible_only: bool = False,
) -> list[dict[str, Any]]:
    needle = (query or "").strip().casefold()
    result: list[dict[str, Any]] = []
    for entry in catalog.get("entries", []):
        if kind != "all" and entry.get("kind") != kind:
            continue
        if origin and entry.get("origin") != origin:
            continue
        if executable_only and not entry.get("executable"):
            continue
        if slot_compatible_only and not entry.get("slot_compatible"):
            continue
        haystack = " ".join(
            str(entry.get(key, "")) for key in ("id", "name", "origin", "author", "cup_type")
        ).casefold()
        if needle and needle not in haystack:
            continue
        result.append(
            {
                key: entry.get(key)
                for key in (
                    "id",
                    "table_id",
                    "name",
                    "kind",
                    "origin",
                    "author",
                    "cup_type",
                    "executable",
                    "slot_compatible",
                    "slots",
                    "warnings",
                )
                if entry.get(key) not in (None, [], "")
            }
        )
    return result


def get_entry(catalog: Mapping[str, Any], identifier: str) -> dict[str, Any]:
    exact = [entry for entry in catalog.get("entries", []) if str(entry.get("id")) == identifier]
    if exact:
        return deepcopy(exact[0])
    by_table = [
        entry
        for entry in catalog.get("entries", [])
        if entry.get("table_id") is not None and str(entry.get("table_id")) == identifier
    ]
    if len(by_table) == 1:
        return deepcopy(by_table[0])
    matches = [
        entry for entry in catalog.get("entries", [])
        if identifier.casefold() in str(entry.get("name", "")).casefold()
    ]
    if len(matches) == 1:
        return deepcopy(matches[0])
    if matches:
        raise CatalogError(f"catalog identifier {identifier!r} is ambiguous ({len(matches)} matches)")
    raise CatalogError(f"catalog entry {identifier!r} was not found")


def export_entry(
    entry: Mapping[str, Any],
    output: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    if not entry.get("executable"):
        reason = "; ".join(entry.get("validation_errors") or ["entry is reference-only"])
        raise CatalogError(f"cannot export {entry.get('id')} as executable YAML: {reason}")
    recipe = deepcopy(entry.get("recipe"))
    if not isinstance(recipe, dict):
        raise CatalogError("catalog entry has no normalised recipe")
    if entry.get("kind") == "tea":
        TeaRecipe.from_dict(recipe)
    else:
        parsed = Recipe.from_dict(recipe)
        strict_validate(parsed)
        recipe = parsed.to_dict()
    resolved = Path(output).expanduser()
    if resolved.suffix.lower() not in {".yaml", ".yml"}:
        raise CatalogError("catalog export path must end in .yaml or .yml")
    if resolved.exists() and not overwrite:
        raise CatalogError(f"refusing to overwrite existing file {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temp = resolved.with_suffix(resolved.suffix + ".tmp")
    temp.write_text(
        yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    temp.replace(resolved)
    return resolved.resolve()


def catalog_summary(catalog: Mapping[str, Any]) -> dict[str, Any]:
    entries = list(catalog.get("entries") or [])
    return {
        "total": len(entries),
        "coffee": sum(entry.get("kind") == "coffee" for entry in entries),
        "tea": sum(entry.get("kind") == "tea" for entry in entries),
        "executable": sum(bool(entry.get("executable")) for entry in entries),
        "slot_compatible": sum(bool(entry.get("slot_compatible")) for entry in entries),
        "updated_at": catalog.get("updated_at"),
    }


def _der_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    if offset >= len(data):
        raise CatalogError("invalid RSA public key")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise CatalogError("invalid RSA public key")
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + count > len(data):
            raise CatalogError("invalid RSA public key")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise CatalogError("invalid RSA public key")
    return tag, data[offset:end], end


def _rsa_public_numbers() -> tuple[int, int]:
    der = base64.b64decode(APP_RSA_PUBLIC_KEY_B64)
    tag, spki, end = _der_tlv(der, 0)
    if tag != 0x30 or end != len(der):
        raise CatalogError("invalid RSA SubjectPublicKeyInfo")
    _, _, offset = _der_tlv(spki, 0)  # algorithm identifier
    tag, bit_string, offset = _der_tlv(spki, offset)
    if tag != 0x03 or offset != len(spki) or not bit_string or bit_string[0] != 0:
        raise CatalogError("invalid RSA public-key bit string")
    tag, rsa_sequence, end = _der_tlv(bit_string[1:], 0)
    if tag != 0x30 or end != len(bit_string) - 1:
        raise CatalogError("invalid RSA public-key sequence")
    tag, modulus_bytes, offset = _der_tlv(rsa_sequence, 0)
    if tag != 0x02:
        raise CatalogError("invalid RSA modulus")
    tag, exponent_bytes, offset = _der_tlv(rsa_sequence, offset)
    if tag != 0x02 or offset != len(rsa_sequence):
        raise CatalogError("invalid RSA exponent")
    return int.from_bytes(modulus_bytes, "big"), int.from_bytes(exponent_bytes, "big")


def _nonzero_random(length: int, randbytes: Callable[[int], bytes]) -> bytes:
    output = bytearray()
    attempts = 0
    while len(output) < length:
        attempts += 1
        chunk = randbytes(length - len(output))
        if not isinstance(chunk, bytes) or not chunk:
            raise CatalogError("RSA random source returned no bytes")
        output.extend(byte for byte in chunk if byte)
        if attempts > 128 and len(output) < length:
            raise CatalogError("RSA random source returned too many zero bytes")
    return bytes(output[:length])


def app_encrypt_form(
    form: Mapping[str, Any],
    *,
    randbytes: Callable[[int], bytes] = secrets.token_bytes,
) -> str:
    """Encode one form exactly like the app's chunked RSA/PKCS#1 v1.5 path."""

    plaintext = json.dumps(form, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    modulus, exponent = _rsa_public_numbers()
    block_size = (modulus.bit_length() + 7) // 8
    chunk_size = block_size - 11
    encrypted = bytearray()
    for start in range(0, len(plaintext), chunk_size):
        chunk = plaintext[start:start + chunk_size]
        padding = _nonzero_random(block_size - len(chunk) - 3, randbytes)
        encoded = b"\x00\x02" + padding + b"\x00" + chunk
        cipher = pow(int.from_bytes(encoded, "big"), exponent, modulus)
        encrypted.extend(cipher.to_bytes(block_size, "big"))
    return base64.b64encode(encrypted).decode("ascii")


def load_cloud_config(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve(strict=True)
    if resolved.stat().st_size > 256 * 1024:
        raise CatalogError("cloud config is unexpectedly large")
    try:
        config = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("cloud config must be valid UTF-8 JSON") from exc
    if not isinstance(config, dict):
        raise CatalogError("cloud config must contain one object")
    region = str(config.get("region", "")).strip().lower()
    aliases = {"intl": "international", "en": "international", "cn": "china", "zh": "china"}
    region = aliases.get(region, region)
    if region not in BASE_URLS:
        raise CatalogError("cloud config region must be international or china")
    config["region"] = region
    base_form = config.get("base_form")
    if not isinstance(base_form, dict):
        raise CatalogError("cloud config requires an app-compatible base_form object")
    base_form.setdefault("pageNumber", 1)
    base_form.setdefault("countPerPage", 0)
    required = {
        "skey",
        "phoneType",
        "appVersion",
        "clientDetail",
        "clientSecretStr",
        "interfaceVersion",
        "token",
        "memberId",
        "clientType",
        "languageType",
    }
    missing = sorted(key for key in required if key not in base_form)
    if missing:
        raise CatalogError(f"cloud base_form is missing app fields: {missing}")
    string_fields = {
        "skey",
        "phoneType",
        "appVersion",
        "clientDetail",
        "clientSecretStr",
        "token",
    }
    invalid_strings = sorted(
        key
        for key in string_fields
        if not isinstance(base_form.get(key), str) or not base_form.get(key)
    )
    if invalid_strings:
        raise CatalogError(
            f"cloud base_form fields must be non-empty strings: {invalid_strings}"
        )
    for key in (
        "interfaceVersion",
        "memberId",
        "clientType",
        "languageType",
        "pageNumber",
        "countPerPage",
    ):
        base_form[key] = _required_int(base_form.get(key), f"cloud base_form {key}")
    return config


def _cloud_request(
    *,
    base_url: str,
    endpoint: str,
    form: Mapping[str, Any],
    timeout: float,
    opener: Callable[..., Any] = urlopen,
) -> Any:
    encrypted = app_encrypt_form(form)
    body = json.dumps(encrypted).encode("utf-8")
    request = Request(
        base_url + endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json;charset=utf-8",
            "User-Agent": "xbloom-studio-brew-catalog/1",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise CatalogError(f"xBloom cloud {endpoint} returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CatalogError(f"xBloom cloud {endpoint} request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CatalogError(f"xBloom cloud {endpoint} response exceeded the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"xBloom cloud {endpoint} returned invalid JSON") from exc


def _normalise_region(region: str) -> str:
    value = str(region).strip().lower()
    aliases = {"intl": "international", "en": "international", "cn": "china", "zh": "china"}
    value = aliases.get(value, value)
    if value not in BASE_URLS:
        raise CatalogError("cloud region must be international or china")
    return value


def _app_base_form(
    *,
    client_secret: str,
    interface_version: int,
    token: str = "",
    member_id: int = 0,
    language_type: int = 0,
) -> dict[str, Any]:
    """Build the fields serialised by Android BaseForm and ProjectForm."""

    if not isinstance(client_secret, str) or not client_secret:
        raise CatalogError("client secret identifier must be a non-empty string")
    parsed_language = _required_int(language_type, "language_type")
    if parsed_language not in {0, 1, 2, 3}:
        raise CatalogError("language_type must be one of the app values 0-3")
    return {
        "skey": APP_SKEY,
        "phoneType": "Android",
        "appVersion": APP_VERSION,
        "clientDetail": "Codex:xbloom-studio-brew",
        "clientSecretStr": client_secret,
        "interfaceVersion": interface_version,
        "token": token,
        "memberId": member_id,
        "clientType": DEFAULT_CLIENT_TYPE,
        "languageType": parsed_language,
        "pageNumber": 1,
        "countPerPage": 0,
    }


def _ephemeral_account_session(
    *,
    email: str,
    password: str,
    region: str,
    language_type: int,
    timeout: float,
    opener: Callable[..., Any],
    client_secret: str | None,
) -> tuple[str, dict[str, Any]]:
    """Login and return an in-memory app session form.

    This private helper deliberately returns no raw login response. Callers must
    not persist or emit the returned form because it contains a bearer token and
    the app's per-install client identifier.
    """

    if not 1 <= float(timeout) <= 60:
        raise CatalogError("catalog sync timeout must be 1-60 seconds")
    if not isinstance(email, str) or not email.strip():
        raise CatalogError("xBloom account email is required")
    if not isinstance(password, str) or not password:
        raise CatalogError("xBloom account password is required")
    resolved_region = _normalise_region(region)
    session_client = client_secret or str(uuid4())
    login_form = _app_base_form(
        client_secret=session_client,
        interface_version=LOGIN_INTERFACE_VERSION,
        language_type=language_type,
    )
    login_form.update(
        {
            "email": email.strip(),
            "password": password,
            "jpushId": "",
        }
    )
    payload = _cloud_request(
        base_url=BASE_URLS[resolved_region],
        endpoint=LOGIN_ENDPOINT,
        form=login_form,
        timeout=float(timeout),
        opener=opener,
    )
    if not isinstance(payload, Mapping) or payload.get("result") != "success":
        result_code = payload.get("resultCode") if isinstance(payload, Mapping) else None
        suffix = f" (resultCode={result_code})" if result_code is not None else ""
        raise CatalogError(f"xBloom account login was rejected{suffix}")
    token = payload.get("token")
    member = payload.get("member")
    if not isinstance(token, str) or not token or not isinstance(member, Mapping):
        raise CatalogError("xBloom account login returned an incomplete session")
    member_id = _optional_int(member.get("tableId"))
    if member_id is None or member_id <= 0:
        raise CatalogError("xBloom account login returned an invalid member session")
    return resolved_region, _app_base_form(
        client_secret=session_client,
        interface_version=CATALOG_INTERFACE_VERSION,
        token=token,
        member_id=member_id,
        language_type=language_type,
    )


def load_cloud_recipe(path: str | Path) -> tuple[Path, Recipe | TeaRecipe]:
    """Load one guarded local recipe for account-cloud preview or upload."""

    resolved = Path(path).expanduser().resolve(strict=True)
    if resolved.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise CatalogError("cloud recipe must be a local .yaml, .yml, or .json file")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CatalogError(f"could not read local recipe {resolved.name}") from exc
    if not isinstance(data, dict):
        raise CatalogError("cloud recipe must be a mapping")
    try:
        if str(data.get("kind", "")).strip().lower() == "tea":
            recipe: Recipe | TeaRecipe = TeaRecipe.from_dict(data)
        else:
            recipe = Recipe.from_dict(data)
            strict_validate(recipe)
    except Exception as exc:
        raise CatalogError(f"local recipe is not cloud-uploadable: {exc}") from exc
    return resolved, recipe


def _app_pour_record(
    *,
    index: int,
    ml: int,
    temp_c: int,
    pattern: str,
    pause_s: int,
    flow_ml_s: float,
    vibration: str = "none",
    label: str | None = None,
) -> dict[str, Any]:
    pattern_name = str(pattern).strip().lower()
    if pattern_name == "ring":
        pattern_name = "circular"
    if pattern_name not in APP_PATTERN_VALUES:
        raise CatalogError(f"pour {index} has no Android app pattern mapping")
    vibration_name = str(vibration).strip().lower()
    if vibration_name not in VIBRATION_LABELS.values():
        raise CatalogError(f"pour {index} has no Android app vibration mapping")
    before = vibration_name in {"before", "both"}
    after = vibration_name in {"after", "both"}
    return {
        "flowRate": float(flow_ml_s),
        "isEnableVibrationAfter": 1 if after else 2,
        "isEnableVibrationBefore": 1 if before else 2,
        "pattern": APP_PATTERN_VALUES[pattern_name],
        "pausing": int(pause_s),
        "recipeId": 0,
        "temperature": float(temp_c),
        "theName": str(label or ("Bloom" if index == 1 else f"Pour{index - 1}")),
        "volume": float(ml),
    }


def build_cloud_recipe_form(
    recipe: Recipe | TeaRecipe,
    *,
    member_id: int | None = None,
) -> dict[str, Any]:
    """Map a guarded recipe to Android 2.2.2's ``RecipeEditForm`` fields."""

    name = str(recipe.name).strip()
    if not name:
        raise CatalogError("cloud recipe name must not be empty")
    if isinstance(recipe, TeaRecipe):
        recipe.validate()
        pours = [
            _app_pour_record(
                index=index,
                ml=pour.ml,
                temp_c=pour.temp_c,
                pattern=pour.pattern,
                pause_s=pour.pause_s,
                flow_ml_s=pour.flow_ml_s,
                label=pour.label,
            )
            for index, pour in enumerate(recipe.pours, start=1)
        ]
        form: dict[str, Any] = {
            "adaptedModel": 1,
            "bypassTemp": 85.0,
            "bypassVolume": 5.0,
            "cupType": 4,
            "dose": float(recipe.leaf_g),
            "grandWater": sum(pour.ml for pour in recipe.pours) / float(recipe.leaf_g),
            "grinderSize": 50.0,
            "isEnableBypassWater": 2,
            "isSetGrinderSize": 2,
            "pourDataJSONStr": json.dumps(
                pours, ensure_ascii=False, separators=(",", ":")
            ),
            "rpm": 120,
            "theColor": DEFAULT_RECIPE_COLOR,
            "theName": name,
        }
        # The current tea editor sets creatorId explicitly. It is resolved only
        # after login so previews remain account/session free.
        if member_id is not None:
            form["creatorId"] = _required_int(member_id, "member id")
        return form

    dripper = str(recipe.dripper or "Omni").strip().casefold()
    if "omni" not in dripper and "xdripper" not in dripper:
        raise CatalogError(
            "only an Omni/xDripper loose-bean recipe can be added to the account; "
            "adapt xPod or other-dripper recipes first"
        )
    rpm_values = {int(pour.rpm) for pour in recipe.pours if int(pour.rpm) > 0}
    if len(rpm_values) > 1:
        raise CatalogError(
            "the Android account recipe schema has one global RPM; local pours use "
            f"multiple values {sorted(rpm_values)}"
        )
    strict_validate(recipe)
    rpm = next(iter(rpm_values), 120)
    pours = [
        _app_pour_record(
            index=index,
            ml=pour.ml,
            temp_c=pour.temp_c,
            pattern=pour.pattern,
            pause_s=pour.pause_s,
            flow_ml_s=pour.flow_ml_s,
            vibration=str(pour.vibration or "none"),
            label=pour.label,
        )
        for index, pour in enumerate(recipe.pours, start=1)
    ]
    enabled_bypass = bool(recipe.bypass_ml)
    form = {
        "adaptedModel": 1,
        "cupType": 2,
        "dose": float(recipe.dose_g),
        "grandWater": float(recipe.effective_ratio),
        "isEnableBypassWater": 1 if enabled_bypass else 2,
        "isSetGrinderSize": 2 if recipe.no_grind else 1,
        "pourDataJSONStr": json.dumps(
            pours, ensure_ascii=False, separators=(",", ":")
        ),
        "rpm": rpm,
        "theColor": DEFAULT_RECIPE_COLOR,
        "theName": name,
    }
    if not recipe.no_grind:
        form["grinderSize"] = float(recipe.grind)
    if enabled_bypass:
        form["bypassVolume"] = float(recipe.bypass_ml)
        form["bypassTemp"] = float(recipe.bypass_temp_c)
    return form


def _cloud_form_semantics(form: Mapping[str, Any]) -> dict[str, Any]:
    """Return the brew-relevant, account-independent part of an app form."""

    pours = _jsonish(form.get("pourDataJSONStr"))
    if not isinstance(pours, list):
        raise CatalogError("cloud recipe form has no pour list")
    semantic_pours: list[dict[str, Any]] = []
    for index, pour in enumerate(pours, start=1):
        if not isinstance(pour, Mapping):
            raise CatalogError(f"cloud recipe pour {index} is not an object")
        semantic_pours.append(
            {
                key: pour.get(key)
                for key in (
                    "flowRate",
                    "isEnableVibrationAfter",
                    "isEnableVibrationBefore",
                    "pattern",
                    "pausing",
                    "temperature",
                    "volume",
                )
            }
        )
    return {
        key: form.get(key)
        for key in (
            "adaptedModel",
            "bypassTemp",
            "bypassVolume",
            "cupType",
            "dose",
            "grandWater",
            "grinderSize",
            "isEnableBypassWater",
            "isSetGrinderSize",
            "rpm",
        )
    } | {"pours": semantic_pours}


def cloud_recipe_preview(recipe: Recipe | TeaRecipe) -> dict[str, Any]:
    """Build a secret-free, non-writing preview of an account recipe add."""

    form = build_cloud_recipe_form(recipe)
    semantics = _cloud_form_semantics(form)
    fingerprint = hashlib.sha256(
        json.dumps(semantics, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    warnings = [
        "preview only; no login or remote write was performed",
        "apply is add-only and refuses a same-name recipe with different parameters",
    ]
    if isinstance(recipe, TeaRecipe):
        warnings.append(
            "tea output_ml_per_steep is display metadata; only each programmed 80/90 ml "
            "steep is uploaded and firmware owns the siphon finish phase"
        )
    return {
        "operation": "idempotent-add",
        "endpoint": RECIPE_ADD_ENDPOINT,
        "kind": "tea" if isinstance(recipe, TeaRecipe) else "coffee",
        "name": str(recipe.name),
        "fingerprint_sha256": fingerprint,
        "app_recipe_form": form,
        "dynamic_account_fields": [
            "token",
            "memberId",
            "clientSecretStr",
            *( ["creatorId"] if isinstance(recipe, TeaRecipe) else [] ),
        ],
        "confirmation_required": CLOUD_WRITE_CONFIRM_SENTINEL,
        "write_performed": False,
        "warnings": warnings,
    }


def push_cloud_recipe_with_login(
    recipe: Recipe | TeaRecipe,
    *,
    email: str,
    password: str,
    region: str,
    confirm_write: str,
    language_type: int = 0,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urlopen,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Idempotently add one local recipe to the member's cloud account.

    This is intentionally add-only. It refuses same-name/different-parameter
    conflicts and never exposes or persists credentials or session fields.
    """

    if confirm_write != CLOUD_WRITE_CONFIRM_SENTINEL:
        raise CatalogError(
            "cloud recipe write requires the exact confirmation "
            f"{CLOUD_WRITE_CONFIRM_SENTINEL!r}"
        )
    preview = cloud_recipe_preview(recipe)
    region_name, session_form = _ephemeral_account_session(
        email=email,
        password=password,
        region=region,
        language_type=language_type,
        timeout=timeout,
        opener=opener,
        client_secret=client_secret,
    )
    created_form = deepcopy(session_form)
    created_form["adaptedModel"] = 1
    created_payload = _cloud_request(
        base_url=BASE_URLS[region_name],
        endpoint=ENDPOINTS["created"],
        form=created_form,
        timeout=float(timeout),
        opener=opener,
    )
    requested_name = str(recipe.name).strip().casefold()
    requested_semantics = _cloud_form_semantics(build_cloud_recipe_form(recipe))
    for raw, context in _candidate_records(created_payload):
        raw_name = str(_first(raw, "theName", "name", default="")).strip()
        if raw_name.casefold() != requested_name:
            continue
        try:
            entry = normalise_entry(
                raw,
                context=context,
                source_type="xbloom-cloud",
                endpoint=ENDPOINTS["created"],
                region=region_name,
            )
            existing_recipe: Recipe | TeaRecipe
            if entry["kind"] == "tea":
                existing_recipe = TeaRecipe.from_dict(entry["recipe"])
            else:
                existing_recipe = Recipe.from_dict(entry["recipe"])
            existing_semantics = _cloud_form_semantics(
                build_cloud_recipe_form(existing_recipe)
            )
        except Exception as exc:
            raise CatalogError(
                "the account already has a same-name recipe that could not be "
                "safely compared; rename the local recipe"
            ) from exc
        if existing_semantics == requested_semantics:
            return {
                "status": "already-present",
                "operation": "idempotent-add",
                "region": region_name,
                "kind": preview["kind"],
                "name": str(recipe.name),
                "remote_table_id": _optional_int(
                    _first(raw, "tableId", "table_id", "recipeId")
                ),
                "write_performed": False,
                "authenticated": True,
                "credentials_persisted": False,
                "session_persisted": False,
            }
        raise CatalogError(
            "the account already has a different recipe with this name; rename "
            "the local recipe instead of overwriting cloud data"
        )

    write_form = deepcopy(session_form)
    write_form["interfaceVersion"] = RECIPE_WRITE_INTERFACE_VERSION
    write_form.update(
        build_cloud_recipe_form(
            recipe,
            member_id=_required_int(session_form.get("memberId"), "member id"),
        )
    )
    payload = _cloud_request(
        base_url=BASE_URLS[region_name],
        endpoint=RECIPE_ADD_ENDPOINT,
        form=write_form,
        timeout=float(timeout),
        opener=opener,
    )
    if not isinstance(payload, Mapping) or payload.get("result") != "success":
        result_code = payload.get("resultCode") if isinstance(payload, Mapping) else None
        suffix = f" (resultCode={result_code})" if result_code is not None else ""
        raise CatalogError(f"xBloom cloud recipe add was rejected{suffix}")
    table_id = _optional_int(payload.get("tableId"))
    if table_id is None or table_id <= 0:
        raise CatalogError("xBloom cloud recipe add returned no remote recipe id")
    return {
        "status": "created",
        "operation": "idempotent-add",
        "region": region_name,
        "kind": preview["kind"],
        "name": str(recipe.name),
        "remote_table_id": table_id,
        "write_performed": True,
        "authenticated": True,
        "credentials_persisted": False,
        "session_persisted": False,
    }


def sync_cloud_with_login(
    catalog: dict[str, Any],
    *,
    email: str,
    password: str,
    region: str = "international",
    include: Iterable[str] = DEFAULT_ACCOUNT_TARGETS,
    language_type: int = 0,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urlopen,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Login ephemerally, sync account-visible recipes, and discard the session.

    The password, token, member id, client id, and raw login response are never
    returned or added to the catalog. Callers must likewise avoid logging the
    input credentials.
    """

    resolved_region, session_form = _ephemeral_account_session(
        email=email,
        password=password,
        region=region,
        language_type=language_type,
        timeout=timeout,
        opener=opener,
        client_secret=client_secret,
    )
    result = sync_cloud(
        catalog,
        {
            "region": resolved_region,
            "adapted_model": 1,
            "base_form": session_form,
        },
        include=include,
        timeout=float(timeout),
        opener=opener,
    )
    return {
        **result,
        "authenticated": True,
        "credentials_persisted": False,
        "session_persisted": False,
    }


def sync_cloud(
    catalog: dict[str, Any],
    config: Mapping[str, Any],
    *,
    include: Iterable[str] = DEFAULT_ACCOUNT_TARGETS,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    if not 1 <= float(timeout) <= 60:
        raise CatalogError("catalog sync timeout must be 1-60 seconds")
    requested = list(dict.fromkeys(str(item) for item in include))
    if not requested:
        raise CatalogError("catalog sync requires at least one target")
    unknown = sorted(set(requested) - set(ENDPOINTS))
    if unknown:
        raise CatalogError(f"unknown catalog sync targets: {unknown}")
    base_form = config.get("base_form")
    if not isinstance(base_form, Mapping):
        raise CatalogError("cloud config has no base_form")
    region = str(config.get("region"))
    if region not in BASE_URLS:
        raise CatalogError("cloud config region must be international or china")
    adapted_model = _required_int(config.get("adapted_model", 1), "adapted_model")
    if adapted_model != 1:
        raise CatalogError("catalog cloud sync supports Studio/J15 adapted_model=1 only")
    easy = config.get("easy_mode")
    results: list[dict[str, Any]] = []
    for target in requested:
        form = deepcopy(dict(base_form))
        if target in {"coffee", "tea", "created", "product", "shared"}:
            form["adaptedModel"] = adapted_model
        else:
            if not isinstance(easy, Mapping):
                raise CatalogError(f"catalog sync target {target} requires easy_mode config")
            if not isinstance(easy.get("sn"), str) or not easy.get("sn"):
                raise CatalogError("easy_mode config requires a non-empty sn")
            for config_key, form_key in (
                ("sn", "sn"),
                ("country_id", "countryId"),
                ("table_id", "tableId"),
            ):
                if config_key not in easy:
                    raise CatalogError(f"easy_mode config is missing {config_key}")
                form[form_key] = (
                    easy[config_key]
                    if config_key == "sn"
                    else _required_int(easy[config_key], f"easy_mode {config_key}")
                )
        endpoint = ENDPOINTS[target]
        payload = _cloud_request(
            base_url=BASE_URLS[region],
            endpoint=endpoint,
            form=form,
            timeout=float(timeout),
            opener=opener,
        )
        candidates = list(_candidate_records(payload, context={}))
        if not candidates and isinstance(payload, Mapping) and payload.get("result") == "success":
            results.append(
                {
                    "target": target,
                    "endpoint": endpoint,
                    "candidates": 0,
                    "added": 0,
                    "updated": 0,
                    "rejected": 0,
                    "rejections": [],
                    "total": len(catalog.get("entries", [])),
                }
            )
            continue
        try:
            stats = import_payload(
                catalog,
                payload,
                source_type="xbloom-cloud",
                endpoint=endpoint,
                region=region,
                kind_hint="tea" if target == "tea" else "auto",
            )
        except CatalogError as exc:
            result_code = payload.get("resultCode") if isinstance(payload, dict) else None
            suffix = f" (resultCode={result_code})" if result_code is not None else ""
            raise CatalogError(f"xBloom cloud {endpoint} returned no importable recipes{suffix}") from exc
        results.append({"target": target, "endpoint": endpoint, **stats})
    return {
        "scope": "own-account-region-visible",
        "region": region,
        "targets": results,
        **catalog_summary(catalog),
    }


__all__ = [
    "ACCOUNT_EMAIL_ENV",
    "ACCOUNT_PASSWORD_ENV",
    "APP_RSA_PUBLIC_KEY_B64",
    "CATALOG_PATH_ENV",
    "CLOUD_CONFIG_ENV",
    "CLOUD_WRITE_CONFIRM_SENTINEL",
    "DEFAULT_ACCOUNT_TARGETS",
    "CatalogError",
    "app_encrypt_form",
    "build_cloud_recipe_form",
    "catalog_summary",
    "cloud_recipe_preview",
    "default_catalog_path",
    "empty_catalog",
    "export_entry",
    "get_entry",
    "import_json_file",
    "import_payload",
    "list_entries",
    "load_catalog",
    "load_cloud_recipe",
    "load_cloud_config",
    "normalise_entry",
    "push_cloud_recipe_with_login",
    "save_catalog",
    "sync_cloud",
    "sync_cloud_with_login",
]

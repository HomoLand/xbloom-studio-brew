"""Guarded xBloom Studio CLI for Agent Skills.

Common flow: doctor -> scan -> probe -> validate -> load -> monitor/cancel.
Physical actions and experimental live adjustment use independent owner gates.

This module stays standard-library-only until ``reexec_in_local_runtime`` hands
off to the external runtime (where ``xbloom-studio-core`` is installed). Path
and re-exec helpers below mirror ``xbloom_paths`` semantics without importing
core, so a clean system Python can still load ``--help`` and re-exec.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import importlib.util
import json
import os
from collections.abc import Mapping
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any


# ---------------------------------------------------------------------------
# Launcher path helpers (stdlib-only; match packages/core/xbloom_paths.py)
# ---------------------------------------------------------------------------

# Mirrors packages/core/xbloom_paths.py (canonical + legacy state env).
STATE_DIR_ENV = "XBLOOM_STATE_DIR"
LEGACY_STATE_DIR_ENV = "XBLOOM_SKILL_STATE_DIR"
RUNTIME_DIR_ENV = "XBLOOM_SKILL_RUNTIME_DIR"
DEFAULT_STATE_DIRNAME = ".xbloom-studio-brew"


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def environment_value(
    name: str,
    default: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read one explicitly named configuration value from the process environment."""

    return _environment(environ).get(name, default)


def environment_copy(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the environment so a child-process overlay cannot mutate its source."""

    return dict(_environment(environ))


def normalize_state_root(path: Path | str) -> Path:
    """Match packages/core/xbloom_paths.normalize_state_root exactly.

    Relative XBLOOM_STATE_DIR values must resolve against cwd so bootstrap,
    re-exec, and core share one absolute state root for a single invocation.
    """

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return candidate.absolute()


def skill_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the user-writable state root, without creating it.

    Precedence: XBLOOM_STATE_DIR > XBLOOM_SKILL_STATE_DIR > default home dir.
    Result is always absolute/normalised (same as core ``normalize_state_root``).
    """

    env = _environment(environ)
    configured = env.get(STATE_DIR_ENV) or env.get(LEGACY_STATE_DIR_ENV)
    if configured:
        return normalize_state_root(configured)
    return normalize_state_root(Path.home() / DEFAULT_STATE_DIRNAME)


def skill_runtime_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the external virtual-environment directory."""

    env = _environment(environ)
    configured = env.get(RUNTIME_DIR_ENV)
    if configured:
        return normalize_state_root(configured)
    return skill_state_dir(env) / "runtime"


def runtime_python_path(runtime_dir: Path) -> Path:
    if os.name == "nt":
        return Path(runtime_dir) / "Scripts" / "python.exe"
    return Path(runtime_dir) / "bin" / "python"


def legacy_runtime_python(skill_root: Path) -> Path:
    """Path used by releases before the runtime moved outside the Skill."""

    return runtime_python_path(Path(skill_root) / ".venv")


def preferred_runtime_python(
    skill_root: Path, environ: Mapping[str, str] | None = None
) -> Path:
    """Prefer the external runtime, with a temporary legacy-install fallback."""

    external = runtime_python_path(skill_runtime_dir(environ))
    if external.exists():
        return external
    legacy = legacy_runtime_python(skill_root)
    return legacy if legacy.exists() else external


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = skill_state_dir()
STATE_FILE = STATE_DIR / "armed-state.json"
TEA_STATE_FILE = STATE_DIR / "tea-loaded-state.json"
GRINDER_STATE_FILE = STATE_DIR / "grinder-rest-state.json"
CATALOG_FILE = STATE_DIR / "catalog" / "catalog.json"
HISTORY_FILE = STATE_DIR / "brew-history.jsonl"
CATALOG_PATH_ENV = "XBLOOM_CATALOG_PATH"
HISTORY_PATH_ENV = "XBLOOM_HISTORY_PATH"
CLOUD_CONFIG_ENV = "XBLOOM_CLOUD_CONFIG"
ACCOUNT_EMAIL_ENV = "XBLOOM_ACCOUNT_EMAIL"
ACCOUNT_PASSWORD_ENV = "XBLOOM_ACCOUNT_PASSWORD"
REMOTE_START_ENV = "XBLOOM_ENABLE_REMOTE_START"
REMOTE_START_SENTINEL = "I_UNDERSTAND_REMOTE_HOT_WATER"
REMOTE_GRINDER_ENV = "XBLOOM_ENABLE_REMOTE_GRINDER"
REMOTE_GRINDER_SENTINEL = "I_UNDERSTAND_REMOTE_GRINDER"
LIVE_ADJUST_ENV = "XBLOOM_ENABLE_LIVE_ADJUST"
LIVE_ADJUST_SENTINEL = "I_ACCEPT_UNVERIFIED_LIVE_ADJUST"
SETTINGS_WRITE_ENV = "XBLOOM_ENABLE_SETTINGS_WRITE"
SETTINGS_WRITE_SENTINEL = "I_ACCEPT_PERSISTENT_MACHINE_SETTINGS"
SETTINGS_CONFIRM_SENTINEL = "persistent-machine-settings"
ADVANCED_CONFIRM_SENTINEL = "mechanical-tuning"
READY_SENTINEL = "cup-filter-water-beans"
WATER_READY_SENTINEL = "vessel-water-clear"
GRINDER_READY_SENTINEL = "beans-cup-clear"
TEA_READY_SENTINEL = "tea-brewer-water-cup-clear"
GRINDER_REST_SECONDS = 60
FIRMWARE_RE = re.compile(rb"V\d+(?:\.\d+[A-Za-z]?)+")
SUPPORTED_FIRMWARE = frozenset({"V12.0D.500"})
UNTESTED_FIRMWARE_ENV = "XBLOOM_ALLOW_UNTESTED_FIRMWARE"
UNTESTED_FIRMWARE_SENTINEL = "I_ACCEPT_UNTESTED_FIRMWARE"
ACTIVE_STATES = frozenset({"armed", "awaiting_confirm", "starting", "brewing", "saving_slots"})
ROOM_TEMPERATURE_C = 20
DEFAULT_PROGRESS_INTERVAL = 1.0
UNCONFIRMED_COMPLETION_EXIT = 3


def local_python() -> Path:
    """External runtime, with a temporary fallback for pre-migration installs."""

    return preferred_runtime_python(ROOT)


def reexec_in_local_runtime() -> None:
    target = local_python()
    if not target.exists():
        return
    try:
        already_local = Path(sys.executable).resolve() == target.resolve()
    except OSError:
        already_local = False
    if already_local or environment_value("XBLOOM_SKILL_REEXEC") == "1":
        return
    env = environment_copy()
    env["XBLOOM_SKILL_REEXEC"] = "1"
    # Preserve the invoking terminal's working directory. Recipe/config paths
    # are user inputs and must not change meaning merely because dependencies
    # are provided by the external runtime.
    raise SystemExit(subprocess.call([str(target), __file__, *sys.argv[1:]], env=env))


def emit(data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    try:
        print(payload, flush=True)
    except UnicodeEncodeError:
        # Windows gateway/terminal sessions may still expose a legacy code page.
        # Preserve valid JSON by escaping only when the active stream cannot
        # represent a device name or decoded diagnostic character.
        print(json.dumps(data, ensure_ascii=True, sort_keys=True), flush=True)


def parse_water_temperature(value: str) -> int:
    """Parse the FreeSolo water temperature CLI value.

    ``RT`` is the official room-temperature/pass-through mode and maps to the
    Android app's 20 C protocol sentinel. Numeric temperatures remain 40-98 C.
    Requiring the token instead of accepting numeric 20 avoids implying that
    Studio actively cools the delivered water to an exact 20 C.
    """
    text = str(value).strip()
    if text.upper() == "RT":
        return ROOM_TEMPERATURE_C
    try:
        temp_c = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "temperature must be RT or an integer from 40 to 98 C"
        ) from exc
    if not 40 <= temp_c <= 98:
        raise argparse.ArgumentTypeError(
            "temperature must be RT or an integer from 40 to 98 C"
        )
    return temp_c


def runtime_ready() -> bool:
    return importlib.util.find_spec("bleak") is not None and importlib.util.find_spec("yaml") is not None


def require_runtime() -> None:
    if not runtime_ready():
        raise RuntimeError("BLE runtime missing; run: python scripts/bootstrap.py")


def state_write(data: dict[str, Any], path: Path | None = None) -> None:
    path = STATE_FILE if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def state_read(path: Path | None = None) -> dict[str, Any]:
    path = STATE_FILE if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"no valid state record at {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"state record at {path} is invalid")
    return data


def state_clear(path: Path | None = None) -> None:
    path = STATE_FILE if path is None else path
    try:
        path.unlink()
    except FileNotFoundError:
        pass




def history_path() -> Path:
    """Deprecated state-root selector path (legacy JSONL location).

    History runtime writes go to state.db via xbloom_history / StateStore.
    ``XBLOOM_HISTORY_PATH`` only selects the associated state root.
    """

    from xbloom_history import default_history_path, resolve_history_state_root

    configured = environment_value(HISTORY_PATH_ENV)
    if configured:
        return default_history_path(resolve_history_state_root(configured))
    return default_history_path(STATE_DIR)


def history_db_path() -> Path:
    """Authoritative brew history store (SQLite state.db)."""

    from xbloom_history import resolve_history_state_root
    from xbloom_storage import DB_FILE_NAME

    return resolve_history_state_root(history_path()) / DB_FILE_NAME


def account_password_for_catalog(action: str) -> str:
    password = environment_value(ACCOUNT_PASSWORD_ENV)
    if password:
        return password
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"catalog {action} requires {ACCOUNT_PASSWORD_ENV} in non-interactive use; "
            "passwords are intentionally not accepted as command arguments"
        )
    import getpass

    return getpass.getpass("xBloom account password: ")


def ensure_no_loaded_workflow() -> None:
    active = [path.name for path in (STATE_FILE, TEA_STATE_FILE) if path.exists()]
    if active:
        raise RuntimeError(
            f"a loaded recipe record exists ({', '.join(active)}); cancel before changing modes"
        )


def require_grinder_rest() -> None:
    if not GRINDER_STATE_FILE.exists():
        return
    try:
        data = state_read(GRINDER_STATE_FILE)
    except RuntimeError as exc:
        raise RuntimeError(
            "grinder rest record is unreadable; do not grind until an owner inspects it"
        ) from exc
    if data.get("in_progress"):
        raise RuntimeError(
            "a previous grinder session has no verified stop; inspect/recover before grinding"
        )
    remaining = float(data.get("blocked_until", 0)) - time.time()
    if remaining > 0:
        raise RuntimeError(
            f"grinder rest interval active; wait {int(remaining + 0.999)} more seconds"
        )


def reserve_grinder_rest(seconds: float) -> None:
    # Reserve before sending START. If the process is killed and cannot write a
    # completion timestamp, the conservative block still covers runtime + rest.
    state_write(
        {
            "reserved_at": time.time(),
            "runtime_s": float(seconds),
            "blocked_until": time.time() + float(seconds) + GRINDER_REST_SECONDS,
        },
        GRINDER_STATE_FILE,
    )


def loaded_workflow_records() -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, state_read(path))
        for path in (STATE_FILE, TEA_STATE_FILE)
        if path.exists()
    ]


def redact_machine_info(machine_info: dict[str, object]) -> dict[str, object]:
    """Return the diagnostic subset safe to place in normal Agent output."""
    return {
        key: value for key, value in machine_info.items() if key != "serial_number"
    }


def make_bridge_client(args: argparse.Namespace | None = None) -> Any:
    """Construct the typed Skill CLI bridge client (ensures daemon on first hardware use)."""

    from xbloom_ble.bridge_client import TypedBridgeClient

    address = None
    if args is not None:
        address = getattr(args, "address", None) or environment_value("XBLOOM_ADDRESS")
    else:
        address = environment_value("XBLOOM_ADDRESS")
    return TypedBridgeClient(address=address, state_root=STATE_DIR)


def require_write_preflight(report: dict[str, Any]) -> str:
    """Legacy helper retained for tests; bridge core enforces firmware gates on write."""

    active = ACTIVE_STATES & set(report.get("states", []))
    if active:
        raise RuntimeError(f"machine is not idle ({', '.join(sorted(active))}); cancel first")
    firmware = set(report.get("firmware", []))
    if firmware and firmware <= SUPPORTED_FIRMWARE:
        return sorted(firmware)[0]
    if environment_value(UNTESTED_FIRMWARE_ENV) == UNTESTED_FIRMWARE_SENTINEL:
        return ",".join(sorted(firmware)) if firmware else "unidentified"
    found = ", ".join(sorted(firmware)) if firmware else "unidentified"
    raise RuntimeError(
        f"firmware {found} is not in the tested set {sorted(SUPPORTED_FIRMWARE)}; "
        f"deployment owner must set {UNTESTED_FIRMWARE_ENV}={UNTESTED_FIRMWARE_SENTINEL} "
        "to accept this risk"
    )


def load_recipe(path: str | Path):
    from xbloom_safety import load_strict_recipe, recipe_summary

    resolved = Path(path).expanduser().resolve(strict=True)
    recipe = load_strict_recipe(resolved)
    return resolved, recipe, recipe_summary(recipe, resolved)


def cmd_doctor(args: argparse.Namespace) -> int:
    bridge_running = False
    if runtime_ready():
        from xbloom_ble.bridge import bridge_is_running as check_bridge_running

        bridge_running = check_bridge_running()

    selected_runtime = local_python()
    external_runtime = runtime_python_path(skill_runtime_dir())
    legacy_runtime = legacy_runtime_python(ROOT)
    catalog_path = Path(environment_value(CATALOG_PATH_ENV, str(CATALOG_FILE))).expanduser()
    cloud_config_value = environment_value(CLOUD_CONFIG_ENV)
    cloud_config_exists = bool(
        cloud_config_value and Path(cloud_config_value).expanduser().is_file()
    )
    account_email_configured = bool(environment_value(ACCOUNT_EMAIL_ENV))
    account_password_configured = bool(environment_value(ACCOUNT_PASSWORD_ENV))
    try:
        runtime_location = (
            "external"
            if selected_runtime.resolve() == external_runtime.resolve()
            else "legacy_skill_local"
        )
    except OSError:
        runtime_location = "external"

    report: dict[str, Any] = {
        "command": "doctor",
        "ok": runtime_ready(),
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
        "runtime_python": str(selected_runtime),
        "runtime_exists": selected_runtime.exists(),
        "runtime_location": runtime_location,
        "state_dir": str(STATE_DIR),
        # Compatibility aliases retained for first-generation Agent consumers.
        "local_runtime": str(selected_runtime),
        "local_runtime_exists": selected_runtime.exists(),
        "legacy_runtime_exists": legacy_runtime.exists(),
        "bleak": importlib.util.find_spec("bleak") is not None,
        "pyyaml": importlib.util.find_spec("yaml") is not None,
        "vendored_protocol": importlib.util.find_spec("xbloom_ble.protocol") is not None,
        "capabilities": {
            "coffee_recipe": True,
            "coffee_bypass": True,
            "coffee_temperature_modes": ["RT", "40-95 C", "BP"],
            "tea_recipe": importlib.util.find_spec("xbloom_ble.tea") is not None,
            "tea_volume_semantics": {
                "stage_ml": "programmed_chamber_fill",
                "approx_120ml_per_steep": "firmware_managed_siphon_finish",
                "generic_recipe_bypass": False,
            },
            "scale": True,
            "grinder": True,
            "temperature_water": True,
            "temperature_water_modes": ["RT", "40-98 C"],
            "water_sources": ["tank", "tap"],
            "water_source_semantics": {"tank": "reservoir", "tap": "direct_feed"},
            "machine_info": True,
            "persistent_bridge": importlib.util.find_spec("xbloom_ble.bridge") is not None,
            "interactive_pause_resume": True,
            "persistent_bridge_operations": [
                "coffee",
                "tea",
                "scale",
                "grinder",
                "water",
                "presets",
                "settings",
                "advanced_tuning",
            ],
            "easy_mode_scope": "atomic_abc_only",
            "private_recipe_catalog": importlib.util.find_spec("xbloom_catalog") is not None,
            "catalog_scope": "own-account-region-visible",
            "catalog_cloud_sync": "ephemeral_login_or_explicit_authorized_app_form",
            "catalog_account_targets": [
                "official-coffee",
                "official-tea",
                "created-coffee-and-tea",
                "product-xpod",
                "shared",
            ],
            "catalog_cloud_push": "preview_default_idempotent_add_only",
            "catalog_cloud_delete": "preview_default_created_tableid_only",
            "catalog_path": str(catalog_path),
            "brew_history": True,
            "brew_history_path": str(history_db_path()),
            "brew_history_source": "state.db",
            "app_brew_history_sync": "ephemeral_login_import_only",
            "catalog_cloud_configured": cloud_config_exists,
            "catalog_login_email_configured": account_email_configured,
            "catalog_login_password_configured": account_password_configured,
            "catalog_login_configured": (
                account_email_configured and account_password_configured
            ),
            "freesolo_live_adjust_protocol": True,
            "freesolo_live_adjust_hardware_verified": False,
            "freesolo_live_pattern_hardware_verified": True,
            "freesolo_live_temperature_command_verified": True,
            "freesolo_live_temperature_outlet_effect_measured": False,
            "freesolo_live_temperature_hardware_verified": False,
            "persistent_settings_protocol": True,
            "persistent_settings_hardware_write_tested": False,
            "advanced_tuning_protocol": True,
            "advanced_tuning_hardware_write_tested": False,
            "four_state_recipe_vibration": ["none", "before", "after", "both"],
            "telemetry": [
                "target_dispensed_water_ml",
                "dispensed_water_ml",
                "cup_weight_g",
                "cup_delta_g",
                "tea_phase",
                "errors",
            ],
        },
        "bridge_running": bridge_running,
        "physical_actions_enabled": {
            "hot_water": environment_value(REMOTE_START_ENV) == REMOTE_START_SENTINEL,
            "grinder": environment_value(REMOTE_GRINDER_ENV) == REMOTE_GRINDER_SENTINEL,
            "live_adjust_unverified": (
                environment_value(LIVE_ADJUST_ENV) == LIVE_ADJUST_SENTINEL
            ),
            "persistent_settings": (
                environment_value(SETTINGS_WRITE_ENV) == SETTINGS_WRITE_SENTINEL
            ),
        },
    }
    if args.scan and runtime_ready():
        from xbloom_ble.client import scan

        devices = asyncio.run(scan(timeout=args.scan_timeout))
        report["machines_found"] = len(devices)
        report["ok"] = report["ok"] and bool(devices)
    emit(report)
    return 0 if report["ok"] else 1


async def async_scan(args: argparse.Namespace) -> int:
    from xbloom_ble.client import scan

    devices = await scan(timeout=args.scan_timeout)
    emit(
        {
            "command": "scan",
            "count": len(devices),
            "machines": [
                {"name": getattr(device, "name", None) or "xBloom", "address": device.address}
                for device in devices
            ],
        }
    )
    return 0 if devices else 1


async def async_probe(args: argparse.Namespace) -> int:
    """Safe one-shot probe via the daemon (never direct Bleak)."""

    if STATE_FILE.exists() or TEA_STATE_FILE.exists():
        raise RuntimeError("a loaded-recipe record exists; use monitor or cancel, not probe")
    client = make_bridge_client(args)
    result = client.probe(
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    emit({"command": "probe", **result})
    return 0


def require_settings_write_gate(confirmation: str, expected: str) -> None:
    if environment_value(SETTINGS_WRITE_ENV) != SETTINGS_WRITE_SENTINEL:
        raise RuntimeError(
            f"persistent machine writes disabled; administrator must set "
            f"{SETTINGS_WRITE_ENV}={SETTINGS_WRITE_SENTINEL}"
        )
    if confirmation != expected:
        raise RuntimeError(f"--confirm-write must equal {expected}")


async def async_settings(args: argparse.Namespace) -> int:
    """Read persistent user settings without changing the machine."""

    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.settings_read(
        address=args.address, scan_timeout=float(args.scan_timeout)
    )
    emit({"command": "settings", **result})
    return 0


async def async_set_settings(args: argparse.Namespace) -> int:
    """Persist selected settings with readback and best-effort rollback."""

    require_settings_write_gate(args.confirm_write, SETTINGS_CONFIRM_SENTINEL)
    requested = {
        key: value
        for key, value in {
            "weight_unit": args.weight_unit,
            "temperature_unit": args.temperature_unit,
            "water_source": args.water_source,
            "display": args.display,
        }.items()
        if value is not None
    }
    if not requested:
        raise RuntimeError("set-settings needs at least one setting option")
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.settings_write(
        confirmation=args.confirm_write,
        address=args.address,
        scan_timeout=float(args.scan_timeout),
        **requested,
    )
    emit({"command": "set-settings", **result})
    return 0


async def async_advanced(args: argparse.Namespace) -> int:
    """Read APK-defined mechanical tuning values."""

    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.advanced_read(
        address=args.address, scan_timeout=float(args.scan_timeout)
    )
    emit({"command": "advanced", **result})
    return 0


async def async_set_advanced(args: argparse.Namespace) -> int:
    """Write UI-level mechanical tuning with exact readback and rollback."""

    require_settings_write_gate(args.confirm_write, ADVANCED_CONFIRM_SENTINEL)
    if args.pour_radius_level is None and args.vibration_level is None:
        raise RuntimeError("set-advanced needs at least one level option")
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.advanced_write(
        confirmation=args.confirm_write,
        pour_radius_level=args.pour_radius_level,
        vibration_level=args.vibration_level,
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    emit({"command": "set-advanced", **result})
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    from xbloom_safety import validate_slot_compatible

    path, recipe, summary = load_recipe(args.recipe)
    if bool(getattr(args, "slot", False)):
        validate_slot_compatible(recipe)
    output = {"command": "validate", "ok": True, "path": str(path), **summary}
    if bool(getattr(args, "slot", False)):
        output["slot_compatible"] = True
    emit(output)
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    """Operate the private, user-local recipe catalog without touching BLE."""

    from xbloom_catalog import (
        ACCOUNT_EMAIL_ENV,
        ACCOUNT_PASSWORD_ENV,
        CLOUD_CONFIG_ENV,
        CLOUD_DELETE_CONFIRM_SENTINEL,
        CLOUD_WRITE_CONFIRM_SENTINEL,
        DEFAULT_ACCOUNT_TARGETS,
        catalog_summary,
        cloud_recipe_delete_preview,
        cloud_recipe_preview,
        default_catalog_path,
        delete_cloud_recipe_with_login,
        export_entry,
        fetch_cloud_brew_records_with_login,
        get_entry,
        import_json_file,
        list_entries,
        load_catalog,
        load_cloud_recipe,
        load_cloud_config,
        push_cloud_recipe_with_login,
        save_catalog,
        sync_cloud,
        sync_cloud_with_login,
    )
    from xbloom_history import import_app_records

    configured_path = getattr(args, "catalog_file", None)
    catalog_path = (
        Path(configured_path).expanduser()
        if configured_path
        else default_catalog_path(STATE_DIR)
    ).resolve()
    catalog = load_catalog(catalog_path)
    action = args.catalog_action
    if action == "status":
        summary = catalog_summary(catalog)
        if not catalog_path.exists():
            summary["updated_at"] = None
        emit(
            {
                "command": "catalog",
                "action": action,
                "path": str(catalog_path),
                "exists": catalog_path.exists(),
                **summary,
            }
        )
        return 0
    if action in {"import-json", "import-mmkv"}:
        stats = import_json_file(
            catalog,
            args.input,
            source_type=("mmkv-json" if action == "import-mmkv" else args.source),
            region=args.region,
            kind_hint=args.kind,
        )
        save_catalog(catalog, catalog_path)
        emit(
            {
                "command": "catalog",
                "action": action,
                "status": "imported",
                "path": str(catalog_path),
                **stats,
            }
        )
        return 0
    if action == "list":
        entries = list_entries(
            catalog,
            kind=args.kind,
            origin=args.origin,
            query=args.query,
            executable_only=bool(args.executable),
            slot_compatible_only=bool(args.slot_compatible),
        )
        emit(
            {
                "command": "catalog",
                "action": action,
                "count": len(entries),
                "entries": entries,
            }
        )
        return 0
    if action == "show":
        emit(
            {
                "command": "catalog",
                "action": action,
                "entry": get_entry(catalog, args.identifier),
            }
        )
        return 0
    if action == "export":
        entry = get_entry(catalog, args.identifier)
        output = export_entry(entry, args.output, overwrite=bool(args.overwrite))
        emit(
            {
                "command": "catalog",
                "action": action,
                "status": "exported",
                "id": entry["id"],
                "kind": entry["kind"],
                "path": str(output),
                "slot_compatible": bool(entry.get("slot_compatible")),
            }
        )
        return 0
    if action == "sync":
        config_value = args.config or environment_value(CLOUD_CONFIG_ENV)
        if not config_value:
            raise RuntimeError(
                f"catalog sync requires --config or {CLOUD_CONFIG_ENV}; "
                "keep the account form outside the Skill/repository"
            )
        config = load_cloud_config(config_value)
        result = sync_cloud(
            catalog,
            config,
            include=args.include or DEFAULT_ACCOUNT_TARGETS,
            timeout=args.timeout,
        )
        save_catalog(catalog, catalog_path)
        emit(
            {
                "command": "catalog",
                "action": action,
                "status": "synced",
                "path": str(catalog_path),
                **result,
            }
        )
        return 0
    if action == "login-sync":
        email = args.email or environment_value(ACCOUNT_EMAIL_ENV)
        if not email:
            raise RuntimeError(
                f"catalog login-sync requires --email or {ACCOUNT_EMAIL_ENV}"
            )
        password = account_password_for_catalog("login-sync")
        result = sync_cloud_with_login(
            catalog,
            email=email,
            password=password,
            region=args.region,
            include=args.include or DEFAULT_ACCOUNT_TARGETS,
            language_type={"en": 0, "zh-cn": 3}[args.language],
            timeout=args.timeout,
        )
        save_catalog(catalog, catalog_path)
        emit(
            {
                "command": "catalog",
                "action": action,
                "status": "synced",
                "path": str(catalog_path),
                **result,
            }
        )
        return 0
    if action == "push":
        recipe_path, recipe = load_cloud_recipe(args.recipe)
        preview = cloud_recipe_preview(recipe)
        if not args.apply:
            emit(
                {
                    "command": "catalog",
                    "action": action,
                    "status": "preview",
                    "recipe_path": str(recipe_path),
                    **preview,
                }
            )
            return 0
        if args.confirm_write != CLOUD_WRITE_CONFIRM_SENTINEL:
            raise RuntimeError(
                "catalog push --apply requires --confirm-write "
                f"{CLOUD_WRITE_CONFIRM_SENTINEL}"
            )
        email = args.email or environment_value(ACCOUNT_EMAIL_ENV)
        if not email:
            raise RuntimeError(
                f"catalog push --apply requires --email or {ACCOUNT_EMAIL_ENV}"
            )
        password = account_password_for_catalog("push")
        result = push_cloud_recipe_with_login(
            recipe,
            email=email,
            password=password,
            region=args.region,
            confirm_write=args.confirm_write,
            language_type={"en": 0, "zh-cn": 3}[args.language],
            timeout=args.timeout,
        )
        emit(
            {
                "command": "catalog",
                "action": action,
                "recipe_path": str(recipe_path),
                **result,
            }
        )
        return 0
    if action == "delete":
        preview = cloud_recipe_delete_preview(
            table_id=getattr(args, "table_id", None),
            identifier=getattr(args, "identifier", None),
            catalog=catalog,
        )
        if not args.apply:
            emit(
                {
                    "command": "catalog",
                    "action": action,
                    "status": "preview",
                    **preview,
                }
            )
            return 0
        if args.confirm_delete != CLOUD_DELETE_CONFIRM_SENTINEL:
            raise RuntimeError(
                "catalog delete --apply requires --confirm-delete "
                f"{CLOUD_DELETE_CONFIRM_SENTINEL}"
            )
        email = args.email or environment_value(ACCOUNT_EMAIL_ENV)
        if not email:
            raise RuntimeError(
                f"catalog delete --apply requires --email or {ACCOUNT_EMAIL_ENV}"
            )
        password = account_password_for_catalog("delete")
        result = delete_cloud_recipe_with_login(
            table_id=int(preview["remote_table_id"]),
            email=email,
            password=password,
            region=args.region,
            confirm_delete=args.confirm_delete,
            expected_name=preview.get("name"),
            language_type={"en": 0, "zh-cn": 3}[args.language],
            timeout=args.timeout,
        )
        if result.get("write_performed"):
            remote_id = result.get("remote_table_id")
            before = len(catalog.get("entries") or [])
            catalog["entries"] = [
                entry
                for entry in catalog.get("entries") or []
                if entry.get("table_id") != remote_id
            ]
            if len(catalog.get("entries") or []) != before:
                save_catalog(catalog, catalog_path)
                result["local_catalog_removed"] = True
            else:
                result["local_catalog_removed"] = False
        emit({"command": "catalog", "action": action, **result})
        return 0
    if action == "history-sync":
        email = args.email or environment_value(ACCOUNT_EMAIL_ENV)
        if not email:
            raise RuntimeError(
                f"catalog history-sync requires --email or {ACCOUNT_EMAIL_ENV}"
            )
        password = account_password_for_catalog("history-sync")
        fetched = fetch_cloud_brew_records_with_login(
            email=email,
            password=password,
            region=args.region,
            language_type={"en": 0, "zh-cn": 3}[args.language],
            timeout=args.timeout,
            keyword=args.keyword or None,
            have_pod=args.have_pod,
        )
        imported = import_app_records(
            fetched.get("records") or [],
            path=history_path(),
            region=fetched.get("region"),
        )
        emit(
            {
                "command": "catalog",
                "action": action,
                "status": "synced",
                "region": fetched.get("region"),
                "fetched": fetched.get("count"),
                "history_path": str(history_db_path()),
                "history_source": "state.db",
                **imported,
                "authenticated": True,
                "credentials_persisted": False,
                "session_persisted": False,
            }
        )
        return 0
    raise RuntimeError(f"unknown catalog action {action}")


def cmd_history(args: argparse.Namespace) -> int:
    """Inspect or annotate the local brew journal without touching BLE."""

    from xbloom_history import HistoryError, add_note, history_summary, list_events

    action = args.history_action
    path = history_path()
    db_path = history_db_path()
    try:
        if action == "status":
            emit({"command": "history", "action": action, **history_summary(path)})
            return 0
        if action == "list":
            events = list_events(
                path=path,
                limit=int(args.limit),
                source=args.source,
                outcome=args.outcome,
                query=args.query,
                recipe_sha256=args.recipe_sha256,
            )
            emit(
                {
                    "command": "history",
                    "action": action,
                    "path": str(db_path),
                    "source": "state.db",
                    "count": len(events),
                    "events": events,
                }
            )
            return 0
        if action == "note":
            event = add_note(args.event_id, args.note, path=path)
            emit(
                {
                    "command": "history",
                    "action": action,
                    "status": "noted",
                    "path": str(db_path),
                    "source": "state.db",
                    "event": event,
                }
            )
            return 0
    except HistoryError as exc:
        raise RuntimeError(str(exc)) from exc
    raise RuntimeError(f"unknown history action {action}")


async def async_load(args: argparse.Namespace) -> int:
    """Load/arm via daemon-owned BLE; returns durable workflow_id."""

    path, _recipe, summary = load_recipe(args.recipe)
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.coffee_load(
        recipe=str(path),
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    workflow_id = result.get("workflow_id")
    if workflow_id is None or not str(workflow_id).strip():
        raise RuntimeError(
            "coffee.load returned no workflow_id; refuse success and do not "
            "write compatibility state (do not retry uncertain load)"
        )
    workflow_id = str(workflow_id).strip()
    state = {
        "address": result.get("address") or args.address or environment_value("XBLOOM_ADDRESS"),
        "machine": result.get("machine"),
        "recipe_path": str(path),
        "recipe_sha256": summary["recipe_sha256"],
        "loaded_at": time.time(),
        "status": "armed",
        "firmware": result.get("firmware"),
        "target_dispensed_water_ml": summary["target_dispensed_water_ml"],
        "serving_kind": summary["kind"],
        "machine_program": summary["machine_program"],
        "manual_preload_ice_g": summary["manual_preload_ice_g"],
        "workflow_id": workflow_id,
    }
    state_write(state)
    # Bridge owns durable terminal history; Skill does not journal load.
    payload = {
        "command": "load",
        "status": result.get("status") or "armed",
        "workflow_id": workflow_id,
        "request_id": result.get("request_id"),
        "remote_start_sent": False,
        **summary,
        **{k: v for k, v in result.items() if k not in {"status"}},
    }
    emit(payload)
    return 0


class _MonitorComplete(Exception):
    pass


@dataclass(frozen=True)
class MonitorResult:
    terminal_confirmed: bool
    terminal_state: str | None
    last_state: str | None
    saw_active: bool
    dispensed_water_ml: float | None
    cup_weight_g: float | None
    scale_g: float | None
    events_seen: int
    elapsed_s: float
    cup_delta_g: float | None = None
    last_report: str | None = None
    tea_phase: str | None = None
    pour_stage: int | None = None
    errors: tuple[str, ...] = ()

    @property
    def completion_confirmed(self) -> bool:
        return self.terminal_confirmed and self.terminal_state in {"ready", "complete"}

    @property
    def water_g(self) -> float | None:
        """Deprecated v1 alias for ``dispensed_water_ml``."""

        return self.dispensed_water_ml

    @property
    def coffee_g(self) -> float | None:
        """Deprecated v1 alias for ``cup_weight_g``."""

        return self.cup_weight_g

    def summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "terminal_confirmed": self.terminal_confirmed,
            "completion_confirmed": self.completion_confirmed,
            "saw_active": self.saw_active,
            "events_seen": self.events_seen,
            "elapsed_s": self.elapsed_s,
        }
        if self.terminal_state is not None:
            data["terminal_state"] = self.terminal_state
        if self.last_state is not None:
            data["last_state"] = self.last_state
        if self.dispensed_water_ml is not None:
            data["dispensed_water_ml"] = self.dispensed_water_ml
            data["water_g"] = self.dispensed_water_ml  # compatibility alias
        if self.cup_weight_g is not None:
            data["cup_weight_g"] = self.cup_weight_g
            data["coffee_g"] = self.cup_weight_g  # compatibility alias
        if self.cup_delta_g is not None:
            data["cup_delta_g"] = self.cup_delta_g
        if self.scale_g is not None:
            data["scale_g"] = self.scale_g
        if self.last_report is not None:
            data["last_report"] = self.last_report
        if self.tea_phase is not None:
            data["tea_phase"] = self.tea_phase
        if self.pour_stage is not None:
            data["pour_stage"] = self.pour_stage
        if self.errors:
            data["errors"] = list(self.errors)
        return data


def volume_comparison(
    state: dict[str, Any], result: MonitorResult
) -> dict[str, float]:
    """Compare recipe target, machine meter, and cup-scale net increase."""
    data: dict[str, float] = {}
    target = state.get("target_dispensed_water_ml")
    if isinstance(target, (int, float)):
        data["target_dispensed_water_ml"] = float(target)
    if result.dispensed_water_ml is not None:
        data["dispensed_water_ml"] = float(result.dispensed_water_ml)
        if "target_dispensed_water_ml" in data:
            data["dispensed_vs_target_ml"] = round(
                float(result.dispensed_water_ml) - data["target_dispensed_water_ml"], 2
            )
    if result.cup_delta_g is not None:
        data["cup_delta_g"] = float(result.cup_delta_g)
        if result.dispensed_water_ml not in (None, 0):
            # Mass and volume are deliberately not subtracted as if they shared
            # units. This ratio is a descriptive g/ml capture indicator only.
            data["cup_delta_to_dispensed_ratio"] = round(
                float(result.cup_delta_g) / float(result.dispensed_water_ml), 4
            )
    return data


def mark_workflow_started(
    state: dict[str, Any], path: Path, machine_state: str
) -> dict[str, Any]:
    updated = dict(state)
    updated.update(
        {
            "status": "running",
            "started_at": time.time(),
            "last_state": machine_state,
        }
    )
    state_write(updated, path)
    return updated


def finalize_workflow_state(
    state: dict[str, Any], path: Path, result: MonitorResult
) -> bool:
    """Clear only a terminal-confirmed workflow; preserve uncertain recovery state."""
    if result.terminal_confirmed:
        state_clear(path)
        return True
    updated = dict(state)
    updated.update(
        {
            "status": "completion_unconfirmed",
            "last_state": result.last_state or state.get("last_state"),
            "last_telemetry_at": time.time(),
        }
    )
    state_write(updated, path)
    return False


async def monitor_client(
    client: Any,
    duration: float,
    *,
    progress_interval: float = DEFAULT_PROGRESS_INTERVAL,
    active_already: bool = False,
) -> MonitorResult:
    if not 0.1 <= float(progress_interval) <= 60:
        raise RuntimeError("progress interval must be 0.1-60 seconds")
    active_states = {0x1F, 0x1E, 0x22, 0x10, 0x23, 0x3B}
    terminal_states = {0x24, 0x41, 0x01}
    saw_active = active_already
    started = time.monotonic()
    last_progress_emit = started
    last_emitted_state: str | None = None
    last_state: str | None = None
    terminal_state: str | None = None
    dispensed_water_ml: float | None = None
    cup_weight_g: float | None = None
    scale_g: float | None = None
    cup_baseline_g: float | None = None
    cup_delta_g: float | None = None
    cup_delta_peak_g: float | None = None
    last_report: str | None = None
    tea_phase: str | None = None
    pour_stage: int | None = None
    error_reports: list[str] = []
    events_seen = 0

    def on_event(event: Any) -> None:
        nonlocal saw_active, last_progress_emit, last_emitted_state, last_state
        nonlocal terminal_state, dispensed_water_ml, cup_weight_g, scale_g, events_seen
        nonlocal cup_baseline_g, cup_delta_g, cup_delta_peak_g
        nonlocal last_report, tea_phase, pour_stage
        events_seen += 1
        now = time.monotonic()
        dispensed = event.dispensed_water_ml
        cup_weight = event.cup_weight_g
        if dispensed is not None:
            dispensed_water_ml = max(dispensed_water_ml or 0.0, float(dispensed))
        if cup_weight is not None:
            cup_weight_g = cup_weight
            value = float(cup_weight)
            cup_baseline_g = value if cup_baseline_g is None else min(cup_baseline_g, value)
            cup_delta_g = round(max(0.0, value - cup_baseline_g), 2)
            cup_delta_peak_g = max(cup_delta_peak_g or 0.0, cup_delta_g)
        if event.scale_g is not None:
            scale_g = event.scale_g
        if event.report_name is not None:
            last_report = event.report_name
        if event.report_name == "tea_soaking":
            tea_phase = "soaking"
        elif event.report_name == "tea_paused":
            tea_phase = "paused"
        elif event.report_name == "tea_restarted":
            tea_phase = "running"
        if event.report_name == "pour_stage" and event.report_value is not None:
            pour_stage = int(event.report_value)
        if getattr(event, "is_error", False) and event.report_name:
            error_reports.append(event.report_name)
        if event.state in active_states:
            saw_active = True
        if event.state is not None:
            last_state = event.state_name
        terminal = saw_active and event.state in terminal_states
        state_changed = event.state is not None and event.state_name != last_emitted_state
        has_weight = any(
            value is not None for value in (dispensed, cup_weight, event.scale_g)
        )
        if terminal or state_changed or (has_weight and now - last_progress_emit >= progress_interval):
            data: dict[str, Any] = {
                "command": "telemetry",
                "time": round(time.time(), 3),
                "state": event.state_name if event.state is not None else (last_state or "scale"),
            }
            if dispensed_water_ml is not None:
                data["dispensed_water_ml"] = dispensed_water_ml
                # Keep the original public field while callers migrate to the
                # more precise name above.
                data["water_g"] = dispensed_water_ml
            if cup_weight_g is not None:
                data["cup_weight_g"] = cup_weight_g
                # Legacy alias: this is the raw cup-scale reading, not coffee
                # beverage mass after subtracting the brewer hardware.
                data["coffee_g"] = cup_weight_g
            if cup_delta_g is not None:
                data["cup_delta_g"] = cup_delta_g
            if scale_g is not None:
                data["scale_g"] = scale_g
            if last_report is not None:
                data["report"] = last_report
            if tea_phase is not None:
                data["tea_phase"] = tea_phase
            if pour_stage is not None:
                data["pour_stage"] = pour_stage
            emit(data)
            last_progress_emit = now
            if event.state is not None:
                last_emitted_state = event.state_name
        if terminal:
            terminal_state = event.state_name
            raise _MonitorComplete()

    terminal_confirmed = False
    try:
        await client.stream_telemetry(on_event, duration=duration, stop_on_terminal=False)
    except _MonitorComplete:
        terminal_confirmed = True
    return MonitorResult(
        terminal_confirmed=terminal_confirmed,
        terminal_state=terminal_state,
        last_state=last_state,
        saw_active=saw_active,
        dispensed_water_ml=dispensed_water_ml,
        cup_weight_g=cup_weight_g,
        scale_g=scale_g,
        events_seen=events_seen,
        elapsed_s=round(time.monotonic() - started, 2),
        cup_delta_g=cup_delta_peak_g,
        last_report=last_report,
        tea_phase=tea_phase,
        pour_stage=pour_stage,
        errors=tuple(dict.fromkeys(error_reports)),
    )


def _monitor_event_state(event: Mapping[str, Any]) -> str | None:
    """Extract a machine/phase state name from live or durable event shapes."""

    if event.get("state"):
        return str(event["state"])
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        for key in ("state", "result", "state_name"):
            if payload.get(key):
                return str(payload[key])
    return None


def _monitor_is_terminal_state(name: str | None) -> bool:
    return name in {"ready", "complete", "idle", "cancelled", "stopped"}


def _monitor_status_matches_workflow(
    status: Mapping[str, Any], workflow_id: str
) -> bool:
    """True only when status/global telemetry can be attributed to workflow_id.

    ``active_workflow_id is None`` is *not* proof of ownership. Use global
    phase/activity/connected/machine_state/telemetry only when:
    - active_workflow_id equals the observed ID, or
    - active is empty AND last_operation.workflow_id equals the observed ID
      (just-terminal provenance).
    """

    active = status.get("active_workflow_id")
    if active is not None and str(active).strip():
        return str(active) == str(workflow_id)
    last_op = status.get("last_operation")
    if isinstance(last_op, Mapping):
        op_wid = last_op.get("workflow_id")
        if op_wid is not None and str(op_wid).strip():
            return str(op_wid) == str(workflow_id)
    return False


async def async_monitor(args: argparse.Namespace) -> int:
    """Observe bridge status/events only — never connect, start, cancel, or release BLE.

    Requires an explicit ``--workflow-id`` or the current active durable workflow
    (no unfiltered all-history mode). ``--duration`` is an observation bound only
    (not recipe expiry). Client exit / Ctrl-C does not alter daemon or BLE ownership.
    """

    if not 0.1 <= float(args.duration) <= 3600:
        raise RuntimeError("monitor --duration must be 0.1-3600 seconds")
    if not 0.1 <= float(args.progress_interval) <= 60:
        raise RuntimeError("monitor --progress-interval must be 0.1-60 seconds")
    client = make_bridge_client(args)
    # Read-only: never ensure/start a daemon or connect BLE.
    workflow_id = getattr(args, "workflow_id", None)
    if workflow_id is not None:
        workflow_id = str(workflow_id).strip() or None
    try:
        status0 = client.status(require_hello=False)
    except Exception as exc:
        raise RuntimeError(
            f"monitor requires a running bridge daemon: {exc}"
        ) from exc
    active_id = status0.get("active_workflow_id")
    if not workflow_id:
        if not active_id:
            raise RuntimeError(
                "monitor requires --workflow-id or an active durable workflow"
            )
        workflow_id = str(active_id)
    else:
        # Reject unknown/stale ID when another workflow is active.
        if active_id and str(active_id) != str(workflow_id):
            raise RuntimeError(
                f"workflow_id {workflow_id!r} is not the active workflow "
                f"{active_id!r}; refuse unfiltered observation"
            )

    emit(
        {
            "command": "monitor",
            "status": "listening",
            "workflow_id": workflow_id,
            "progress_interval_s": args.progress_interval,
            "observation_only": True,
            "bridge_owned": True,
            "daemon_untouched": True,
        }
    )
    deadline = time.monotonic() + float(args.duration)
    since = 0
    last_progress = 0.0
    terminal_state: str | None = None
    last_status = status0
    # Independent consecutive-failure counters: success on one channel must not
    # reset failures on the other (avoids swallowing a permanently failing status).
    events_failures = 0
    status_failures = 0
    fail_threshold = 3
    while time.monotonic() < deadline:
        try:
            page = client.events(since=since, workflow_id=workflow_id)
            events_failures = 0
        except Exception as exc:
            events_failures += 1
            if events_failures >= fail_threshold:
                raise RuntimeError(
                    f"monitor events failed repeatedly: {exc}"
                ) from exc
            await asyncio.sleep(min(0.5, float(args.progress_interval)))
            continue
        if page.get("gap_detected"):
            raise RuntimeError(
                f"monitor event gap for workflow {workflow_id!r}: "
                f"{page.get('gap_reason') or 'gap_detected'}"
            )
        events = list(page.get("events") or [])
        since = int(page.get("next_since") or since)
        for event in events:
            # Durable rows use event_type + payload; live rows use state.
            event_type = event.get("event_type")
            state_name = _monitor_event_state(event)
            if event_type == "terminal" or _monitor_is_terminal_state(state_name):
                terminal_state = state_name or (
                    str((event.get("payload") or {}).get("result"))
                    if isinstance(event.get("payload"), Mapping)
                    else None
                ) or "terminal"
        try:
            last_status = client.status(require_hello=False)
            status_failures = 0
        except Exception as exc:
            status_failures += 1
            if status_failures >= fail_threshold:
                raise RuntimeError(
                    f"monitor status failed repeatedly: {exc}"
                ) from exc
            await asyncio.sleep(min(0.5, float(args.progress_interval)))
            continue
        # Provenance-safe attachment of global status fields.
        same_workflow = _monitor_status_matches_workflow(last_status, str(workflow_id))
        now = time.monotonic()
        if now - last_progress >= float(args.progress_interval):
            progress: dict[str, Any] = {
                "command": "monitor-progress",
                "workflow_id": workflow_id,
                "time": round(time.time(), 3),
            }
            if same_workflow:
                progress["phase"] = last_status.get("phase")
                progress["activity"] = last_status.get("activity")
                progress["connected"] = last_status.get("connected")
                progress["machine_state"] = last_status.get("machine_state")
                if last_status.get("telemetry") is not None:
                    progress["telemetry"] = last_status.get("telemetry")
                if last_status.get("liquid_progress") is not None:
                    progress["liquid_progress"] = last_status.get("liquid_progress")
            emit(progress)
            last_progress = now
        if same_workflow:
            phase = last_status.get("phase")
            if phase in {"idle", "disconnected"} and last_status.get("activity") is None:
                last_op = last_status.get("last_operation")
                if isinstance(last_op, Mapping) and str(
                    last_op.get("workflow_id") or ""
                ) == str(workflow_id):
                    terminal_state = terminal_state or str(
                        last_op.get("result") or phase
                    )
                    break
                if last_status.get("active_workflow_id") is None and last_op is None:
                    # No competing identity and no last_op; durable events decide.
                    pass
        if terminal_state:
            break
        await asyncio.sleep(min(0.5, float(args.progress_interval)))
    payload: dict[str, Any] = {
        "command": "monitor",
        "status": terminal_state or "duration_elapsed",
        "workflow_id": workflow_id,
        "observation_only": True,
        "daemon_untouched": True,
    }
    if _monitor_status_matches_workflow(last_status, str(workflow_id)):
        payload["phase"] = last_status.get("phase")
        payload["activity"] = last_status.get("activity")
        payload["connected"] = last_status.get("connected")
        payload["machine_state"] = last_status.get("machine_state")
        payload["telemetry"] = last_status.get("telemetry")
        payload["liquid_progress"] = last_status.get("liquid_progress")
    emit(payload)
    return 0


async def async_scale(args: argparse.Namespace) -> int:
    """Standalone scale via bridge one-shot workflow."""

    if not 0.05 <= float(args.interval) <= 10.0:
        raise RuntimeError("scale --interval must be 0.05-10 seconds")
    if not 0.1 <= float(args.duration) <= 3600:
        raise RuntimeError("scale --duration must be 0.1-3600 seconds")
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    emit(
        {
            "command": "scale",
            "status": "entering",
            "entry_auto_zero": True,
            "extra_tare_requested": bool(args.tare),
        }
    )
    result = client.scale_start(
        duration_s=float(args.duration),
        tare=bool(args.tare),
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    emit({"command": "scale", "status": "started", **result})
    # Observation bound only: poll status until activity ends or duration elapses.
    deadline = time.monotonic() + float(args.duration) + 2.0
    while time.monotonic() < deadline:
        st = client.status(require_hello=False)
        if st.get("activity") != "scale":
            break
        await asyncio.sleep(max(0.2, float(args.interval)))
    emit({"command": "scale", "status": "exited", **result})
    return 0


async def async_grind(args: argparse.Namespace) -> int:
    if environment_value(REMOTE_GRINDER_ENV) != REMOTE_GRINDER_SENTINEL:
        raise RuntimeError(
            f"remote grinder disabled; administrator must set "
            f"{REMOTE_GRINDER_ENV}={REMOTE_GRINDER_SENTINEL}"
        )
    if args.confirm_ready != GRINDER_READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {GRINDER_READY_SENTINEL}")
    if not 1 <= int(args.size) <= 80:
        raise RuntimeError("grind --size must be 1-80")
    if not 60 <= int(args.rpm) <= 120:
        raise RuntimeError("grind --rpm must be 60-120")
    if not 0.1 <= float(args.seconds) <= 30.0:
        raise RuntimeError("grind --seconds must be 0.1-30")
    ensure_no_loaded_workflow()
    require_grinder_rest()
    reserve_grinder_rest(args.seconds)
    client = make_bridge_client(args)
    result = client.grinder_start(
        size=int(args.size),
        rpm=int(args.rpm),
        seconds=float(args.seconds),
        confirmation=args.confirm_ready,
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    emit(
        {
            "command": "grind",
            "status": result.get("status") or "stopped",
            "size": args.size,
            "rpm": args.rpm,
            "seconds": args.seconds,
            "rest_seconds": GRINDER_REST_SECONDS,
            "workflow_id": result.get("workflow_id"),
            **result,
        }
    )
    return 0


async def async_water(args: argparse.Namespace) -> int:
    if environment_value(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"hot-water actions disabled; administrator must set "
            f"{REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != WATER_READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {WATER_READY_SENTINEL}")
    if not 20 <= float(args.volume) <= 360:
        raise RuntimeError("water --volume must be 20-360 ml")
    if int(args.temp) != ROOM_TEMPERATURE_C and not 40 <= int(args.temp) <= 98:
        raise RuntimeError("water --temp must be RT or 40-98 C")
    flow10 = round(float(args.flow) * 10)
    if flow10 not in range(30, 36) or abs(flow10 / 10 - float(args.flow)) > 1e-6:
        raise RuntimeError("water --flow must be 3.0-3.5 ml/s in 0.1 steps")
    if args.timeout is not None and not 5 <= float(args.timeout) <= 600:
        raise RuntimeError("water --timeout must be 5-600 seconds")
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.water_start(
        volume_ml=float(args.volume),
        temp_c=int(args.temp),
        flow_ml_s=float(args.flow),
        pattern=args.pattern,
        water_source=args.water_source,
        confirmation=args.confirm_ready,
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    workflow_id = result.get("workflow_id")
    if workflow_id is None or not str(workflow_id).strip():
        raise RuntimeError(
            "water.start returned no workflow_id; refuse observation and do not "
            "claim completion (do not retry uncertain start)"
        )
    workflow_id = str(workflow_id).strip()
    # --timeout is an observation bound only (never cancel/release the daemon).
    if args.timeout is not None:
        observe_s = float(args.timeout)
    else:
        safety = result.get("safety_timeout_s")
        try:
            observe_s = float(safety) + 30.0 if safety is not None else 300.0
        except (TypeError, ValueError):
            observe_s = 300.0
        observe_s = max(5.0, min(600.0, observe_s))
    emit(
        {
            "command": "water",
            "status": result.get("status") or "running",
            "target_dispensed_water_ml": args.volume,
            "temp_c": args.temp,
            "temp_setting": (
                "RT" if args.temp == ROOM_TEMPERATURE_C else f"{args.temp} C"
            ),
            "flow_ml_s": args.flow,
            "pattern": args.pattern,
            "workflow_id": workflow_id,
            "observation_bound_s": observe_s,
            **result,
        }
    )
    # Observe the exact workflow until terminal or bound; never cancel on exit.
    observe_args = SimpleNamespace(
        duration=observe_s,
        progress_interval=min(1.0, max(0.1, observe_s / 10.0)),
        workflow_id=workflow_id,
        address=args.address,
        scan_timeout=args.scan_timeout,
    )
    await async_monitor(observe_args)
    return 0


def load_tea_recipe(path: str | Path):
    from xbloom_ble.tea import TeaRecipe
    from xbloom_safety import recipe_sha256

    resolved = Path(path).expanduser().resolve(strict=True)
    recipe = TeaRecipe.from_yaml(resolved)
    summary = recipe.summary()
    summary["recipe_sha256"] = recipe_sha256(resolved)
    return resolved, recipe, summary


def cmd_tea_validate(args: argparse.Namespace) -> int:
    path, _recipe, summary = load_tea_recipe(args.recipe)
    emit({"command": "tea-validate", "ok": True, "path": str(path), **summary})
    return 0


async def async_tea_load(args: argparse.Namespace) -> int:
    path, _recipe, summary = load_tea_recipe(args.recipe)
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    result = client.tea_load(
        recipe=str(path),
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    workflow_id = result.get("workflow_id")
    if workflow_id is None or not str(workflow_id).strip():
        raise RuntimeError(
            "tea.load returned no workflow_id; refuse success and do not "
            "write compatibility state (do not retry uncertain load)"
        )
    workflow_id = str(workflow_id).strip()
    state = {
        "address": result.get("address") or args.address or environment_value("XBLOOM_ADDRESS"),
        "recipe_path": str(path),
        "recipe_sha256": summary["recipe_sha256"],
        "loaded_at": time.time(),
        "status": "tea_loaded",
        "firmware": result.get("firmware"),
        "target_dispensed_water_ml": summary["programmed_water_ml"],
        "serving_kind": "tea",
        "machine_program": "omni-tea-brewer",
        "workflow_id": workflow_id,
    }
    state_write(state, TEA_STATE_FILE)
    # Bridge owns durable terminal history; Skill does not journal tea-load.
    payload = {
        "command": "tea-load",
        "status": result.get("status") or "tea_loaded",
        "workflow_id": workflow_id,
        "remote_start_sent": False,
        **summary,
        **result,
    }
    emit(payload)
    return 0


async def async_tea_start(args: argparse.Namespace) -> int:
    from xbloom_safety import recipe_sha256

    if environment_value(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"hot-water actions disabled; administrator must set "
            f"{REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != TEA_READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {TEA_READY_SENTINEL}")
    if not 1 <= float(args.duration) <= 3600:
        raise RuntimeError("tea-start --duration must be 1-3600 seconds")
    path, _recipe, summary = load_tea_recipe(args.recipe)
    state = state_read(TEA_STATE_FILE) if TEA_STATE_FILE.exists() else {}
    if state and state.get("recipe_sha256") not in (None, recipe_sha256(path)):
        raise RuntimeError("tea recipe changed since it was loaded")
    client = make_bridge_client(args)
    workflow_id = getattr(args, "workflow_id", None) or state.get("workflow_id")
    if not workflow_id:
        workflow_id = client.resolve_active_workflow_id(
            kind="tea",
            allowed_phases={"loaded", "tea_loaded"},
        )
    result = client.tea_start(
        workflow_id=str(workflow_id),
        confirmation=args.confirm_ready,
    )
    if state:
        state = mark_workflow_started(state, TEA_STATE_FILE, "start_accepted")
    emit(
        {
            "command": "tea-start",
            "status": result.get("status") or "start_accepted",
            "workflow_id": workflow_id,
            "recipe_sha256": summary["recipe_sha256"],
            **result,
        }
    )
    # Observation-only poll; does not connect or cancel the daemon workflow.
    args.workflow_id = workflow_id
    await async_monitor(args)
    return 0


async def async_tea_brew(args: argparse.Namespace) -> int:
    """Load then start tea over the same daemon-owned BLE link."""

    if environment_value(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"hot-water actions disabled; administrator must set "
            f"{REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != TEA_READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {TEA_READY_SENTINEL}")
    if not 1 <= float(args.duration) <= 3600:
        raise RuntimeError("tea-brew --duration must be 1-3600 seconds")

    path, _recipe, summary = load_tea_recipe(args.recipe)
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    loaded = client.tea_load(
        recipe=str(path),
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    workflow_id = loaded.get("workflow_id")
    if not workflow_id:
        raise RuntimeError("tea.load did not return workflow_id")
    state = {
        "address": args.address or environment_value("XBLOOM_ADDRESS"),
        "recipe_path": str(path),
        "recipe_sha256": summary["recipe_sha256"],
        "loaded_at": time.time(),
        "status": "tea_loaded",
        "firmware": loaded.get("firmware"),
        "target_dispensed_water_ml": summary["programmed_water_ml"],
        "workflow_id": workflow_id,
    }
    state_write(state, TEA_STATE_FILE)
    emit(
        {
            "command": "tea-brew",
            "status": "tea_loaded",
            "workflow_id": workflow_id,
            **summary,
            **loaded,
        }
    )
    started = client.tea_start(
        workflow_id=str(workflow_id),
        confirmation=args.confirm_ready,
    )
    mark_workflow_started(state, TEA_STATE_FILE, "start_accepted")
    emit(
        {
            "command": "tea-brew",
            "status": started.get("status") or "start_accepted",
            "workflow_id": workflow_id,
            **started,
        }
    )
    args.workflow_id = workflow_id
    await async_monitor(args)
    return 0


async def async_cancel(args: argparse.Namespace) -> int:
    """Cancel a durable workflow. Emergency requires explicit ``--emergency``."""

    client = make_bridge_client(args)
    prior_state = None
    workflow_id = getattr(args, "workflow_id", None)
    emergency = bool(getattr(args, "emergency", False))
    for path in (STATE_FILE, TEA_STATE_FILE):
        if path.exists():
            try:
                prior_state = state_read(path)
                if not workflow_id:
                    workflow_id = prior_state.get("workflow_id")
                break
            except RuntimeError:
                pass
    if not workflow_id and not emergency:
        try:
            workflow_id = client.resolve_active_workflow_id()
        except Exception as exc:
            raise RuntimeError(
                "cancel requires --workflow-id or an active durable workflow "
                "(pass --emergency only for explicit emergency stop)"
            ) from exc
    if not workflow_id and not emergency:
        raise RuntimeError(
            "cancel requires --workflow-id or an active durable workflow "
            "(pass --emergency only for explicit emergency stop)"
        )
    result = client.cancel(
        workflow_id=str(workflow_id) if workflow_id else None,
        emergency=emergency,
    )
    state_clear()
    state_clear(TEA_STATE_FILE)
    # Bridge commit_workflow_terminal owns the one final history row for cancel.
    payload = {
        "command": "cancel",
        "status": result.get("status") or "cancel_sent",
        "workflow_id": workflow_id,
        "emergency": emergency,
        "coffee_state_cleared": True,
        "tea_state_cleared": True,
        **result,
    }
    emit(payload)
    return 0


async def async_start(args: argparse.Namespace) -> int:
    from xbloom_safety import recipe_sha256

    if environment_value(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"remote start disabled; administrator must set {REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {READY_SENTINEL}")
    if not 1 <= float(args.duration) <= 3600:
        raise RuntimeError("start --duration must be 1-3600 seconds")
    path, _recipe, summary = load_recipe(args.recipe)
    state = state_read() if STATE_FILE.exists() else {}
    state_status = state.get("status")
    if state_status in {"start_pending", "start_unconfirmed"}:
        raise RuntimeError(
            "previous start outcome is unconfirmed; run monitor or cancel; do not retry"
        )
    if state and state.get("recipe_sha256") not in (None, recipe_sha256(path)):
        raise RuntimeError("recipe changed since it was loaded")
    client = make_bridge_client(args)
    workflow_id = getattr(args, "workflow_id", None) or state.get("workflow_id")
    if not workflow_id:
        workflow_id = client.resolve_active_workflow_id(
            kind="coffee",
            allowed_phases={"loaded", "armed"},
        )
    if state:
        state = dict(state)
        state.update(
            status="start_pending",
            start_requested_at=time.time(),
            workflow_id=workflow_id,
        )
        state_write(state, STATE_FILE)
    result = client.coffee_start(
        workflow_id=str(workflow_id),
        confirmation=args.confirm_ready,
    )
    if state:
        mark_workflow_started(state, STATE_FILE, str(result.get("status") or "running"))
    emit(
        {
            "command": "start",
            "status": result.get("status") or "running",
            "workflow_id": workflow_id,
            "recipe_sha256": summary["recipe_sha256"],
            "machine_program": summary.get("machine_program", "coffee-pour-over"),
            "machine_dispenses_ice": bool(
                summary.get("machine_dispenses_ice", False)
            ),
            "manual_preload_ice_g": int(
                summary.get("manual_preload_ice_g", 0) or 0
            ),
            **result,
        }
    )
    # Observation bound only — does not release BLE or shut down the daemon.
    args.workflow_id = workflow_id
    await async_monitor(args)
    return 0


async def async_save_slots(args: argparse.Namespace) -> int:
    from xbloom_safety import validate_slot_compatible

    loaded = [load_recipe(path) for path in args.recipes]
    for _path, recipe, _summary in loaded:
        validate_slot_compatible(recipe)
    ensure_no_loaded_workflow()
    client = make_bridge_client(args)
    scale = [value == "on" for value in args.scale]
    result = client.presets_save(
        recipes=[str(item[0]) for item in loaded],
        scale=scale,
        address=args.address,
        scan_timeout=float(args.scan_timeout),
    )
    emit(
        {
            "command": "save-slots",
            "status": result.get("status") or "saved",
            "slots": [item[2]["name"] for item in loaded],
            "scale": dict(zip(("A", "B", "C"), scale)),
            "brew_started": False,
            **result,
        }
    )
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """Explicit state.db migration/status/backup — never auto-migrates on daemon start."""

    import xbloom_storage as storage

    action = args.state_action
    if action == "status":
        result = storage.migration_status(STATE_DIR)
    elif action == "migrate":
        result = storage.migrate_legacy_state(
            STATE_DIR,
            backup_root=Path(args.backup_root) if getattr(args, "backup_root", None) else None,
            force=bool(getattr(args, "force", False)),
        )
    elif action == "backup":
        store = storage.StateStore(STATE_DIR)
        store.ensure_schema()
        dest = store.backup(
            Path(args.destination) if getattr(args, "destination", None) else None
        )
        result = {
            "command": "state",
            "action": "backup",
            "status": "backed_up",
            "destination": str(dest),
            "state_root": str(store.state_root),
            "runtime_source_of_truth": {
                "workflow": "sqlite",
                "history": "sqlite",
                "idempotency": "sqlite",
                "catalog": "json_legacy",
            },
            "message": (
                "online SQLite backup only; workflow/history/idempotency use state.db; "
                "catalog remains JSON-backed"
            ),
        }
        store.close()
    else:
        raise RuntimeError(f"unknown state action {action}")
    emit({"command": "state", "action": action, **result})
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from xbloom_ble.bridge import (
        BridgeError,
        bridge_is_running,
        bridge_record_path,
        ensure_bridge_daemon,
        restart_bridge_daemon_if_idle,
        serve_bridge,
        stop_bridge_daemon,
    )

    action = args.bridge_action
    if action == "serve":
        asyncio.run(serve_bridge(address=args.address))
        return 0
    if action == "start":
        result = ensure_bridge_daemon(address=args.address)
        emit({"command": "bridge", "action": action, **result})
        return 0
    if action == "stop":
        result = stop_bridge_daemon(force=bool(args.force))
        emit({"command": "bridge", "action": action, **result})
        return 0
    if action == "restart-if-idle":
        result = restart_bridge_daemon_if_idle(address=args.address)
        emit({"command": "bridge", "action": action, **result})
        return 0

    client = make_bridge_client(args)
    if action == "status":
        if not bridge_is_running():
            result = {
                "running": False,
                "connected": False,
                "record": str(bridge_record_path()),
            }
        else:
            result = client.status(require_hello=False)
    elif action == "connect":
        result = client.connect(
            address=args.address, scan_timeout=float(args.scan_timeout)
        )
    elif action == "disconnect":
        result = client.disconnect()
    elif action == "events":
        result = client.events(since=int(args.since))
    elif action == "settings":
        result = client.settings_read(
            address=args.address, scan_timeout=float(args.scan_timeout)
        )
    elif action == "set-settings":
        result = client.settings_write(
            confirmation=args.confirm_write,
            weight_unit=args.weight_unit,
            temperature_unit=args.temperature_unit,
            water_source=args.water_source,
            display=args.display,
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "advanced":
        result = client.advanced_read(
            address=args.address, scan_timeout=float(args.scan_timeout)
        )
    elif action == "set-advanced":
        result = client.advanced_write(
            confirmation=args.confirm_write,
            pour_radius_level=args.pour_radius_level,
            vibration_level=args.vibration_level,
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "coffee-load":
        recipe = str(Path(args.recipe).expanduser().resolve(strict=True))
        result = client.coffee_load(
            recipe=recipe,
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "coffee-start":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id(
            kind="coffee", allowed_phases={"loaded", "armed"}
        )
        result = client.coffee_start(
            workflow_id=str(wid),
            confirmation=args.confirm_ready,
            timeout=float(getattr(args, "timeout", 60.0) or 60.0),
        )
    elif action == "tea-load":
        recipe = str(Path(args.recipe).expanduser().resolve(strict=True))
        result = client.tea_load(
            recipe=recipe,
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "tea-start":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id(
            kind="tea", allowed_phases={"loaded", "tea_loaded"}
        )
        result = client.tea_start(
            workflow_id=str(wid),
            confirmation=args.confirm_ready,
            timeout=float(getattr(args, "timeout", 60.0) or 60.0),
        )
    elif action == "scale-start":
        result = client.scale_start(
            duration_s=float(args.duration),
            tare=bool(args.tare),
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "scale-tare":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id(
            kind="scale"
        )
        result = client.scale_tare(workflow_id=str(wid))
    elif action == "save-slots":
        recipes = [
            str(Path(path).expanduser().resolve(strict=True)) for path in args.recipes
        ]
        result = client.presets_save(
            recipes=recipes,
            scale=[value == "on" for value in args.scale],
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "grinder-start":
        result = client.grinder_start(
            size=int(args.size),
            rpm=int(args.rpm),
            seconds=float(args.seconds),
            confirmation=args.confirm_ready,
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "water-start":
        result = client.water_start(
            volume_ml=float(args.volume),
            temp_c=int(args.temp),
            flow_ml_s=float(args.flow),
            pattern=args.pattern,
            water_source=args.water_source,
            confirmation=args.confirm_ready,
            address=args.address,
            scan_timeout=float(args.scan_timeout),
        )
    elif action == "pause":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id()
        result = client.pause(workflow_id=str(wid))
    elif action == "resume":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id()
        result = client.resume(workflow_id=str(wid))
    elif action == "cancel":
        wid = getattr(args, "workflow_id", None)
        emergency = bool(getattr(args, "emergency", False))
        if not wid and not emergency:
            try:
                wid = client.resolve_active_workflow_id()
            except Exception as exc:
                raise BridgeError(
                    "cancel requires --workflow-id or an active durable workflow "
                    "(pass --emergency only for explicit emergency stop)"
                ) from exc
        if not wid and not emergency:
            raise BridgeError(
                "cancel requires --workflow-id or an active durable workflow "
                "(pass --emergency only for explicit emergency stop)"
            )
        result = client.cancel(
            workflow_id=str(wid) if wid else None,
            emergency=emergency,
        )
    elif action == "water-temperature":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id(
            kind="water"
        )
        result = client.water_set_temperature(
            workflow_id=str(wid),
            temp_c=int(args.temp),
            confirmation=args.confirm_live_adjust,
        )
    elif action == "water-pattern":
        wid = getattr(args, "workflow_id", None) or client.resolve_active_workflow_id(
            kind="water"
        )
        result = client.water_set_pattern(
            workflow_id=str(wid),
            pattern=args.pattern,
            confirmation=args.confirm_live_adjust,
        )
    else:  # pragma: no cover - argparse guarantees the action
        raise BridgeError(f"unknown bridge action {action}")
    emit({"command": "bridge", "action": action, **result})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="BLE address/identifier; defaults to XBLOOM_ADDRESS or scan")
    parser.add_argument("--scan-timeout", type=float, default=8.0)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the local runtime")
    doctor.add_argument("--scan", action="store_true", help="also scan without connecting")
    sub.add_parser("scan", help="discover nearby xBloom machines without writing")
    sub.add_parser("probe", help="safe session/status probe; never use while a recipe is armed")
    sub.add_parser("settings", help="read persistent unit, water-source, and display settings")
    set_settings = sub.add_parser(
        "set-settings", help="explicitly gated persistent machine settings with readback"
    )
    set_settings.add_argument("--weight-unit", choices=("ml", "g", "oz"))
    set_settings.add_argument("--temperature-unit", choices=("C", "F"))
    set_settings.add_argument("--water-source", choices=("tank", "tap"))
    set_settings.add_argument("--display", choices=("low", "medium", "high"))
    set_settings.add_argument("--confirm-write", default="")
    sub.add_parser("advanced", help="read pour-radius and vibration-amplitude tuning")
    set_advanced = sub.add_parser(
        "set-advanced", help="explicitly gated mechanical tuning with readback"
    )
    set_advanced.add_argument("--pour-radius-level", type=int, choices=range(1, 6))
    set_advanced.add_argument("--vibration-level", type=int, choices=range(1, 7))
    set_advanced.add_argument("--confirm-write", default="")
    validate = sub.add_parser("validate", help="strictly validate a local recipe")
    validate.add_argument("recipe")
    validate.add_argument(
        "--slot",
        action="store_true",
        help="also require lossless compatibility with Auto/Easy A/B/C storage",
    )
    catalog = sub.add_parser(
        "catalog",
        help="import, sync, query, and export a private coffee/tea recipe catalog",
    )
    catalog.add_argument(
        "--catalog-file",
        help="private catalog JSON path; defaults below the writable Skill state directory",
    )
    catalog_sub = catalog.add_subparsers(dest="catalog_action", required=True)
    catalog_sub.add_parser("status", help="show catalog location and counts; never uses network")
    catalog_import = catalog_sub.add_parser(
        "import-json", help="import an authorised xBloom App/API JSON export"
    )
    catalog_import.add_argument("input")
    catalog_import.add_argument(
        "--source",
        choices=("app-json", "api-json", "public-share-json"),
        default="app-json",
    )
    catalog_import.add_argument("--region", choices=("international", "china"))
    catalog_import.add_argument("--kind", choices=("auto", "coffee", "tea"), default="auto")
    catalog_mmkv = catalog_sub.add_parser(
        "import-mmkv",
        help="import a decoded MMKV JSON dump (raw MMKV binary is not accepted)",
    )
    catalog_mmkv.add_argument("input")
    catalog_mmkv.add_argument("--region", choices=("international", "china"))
    catalog_mmkv.add_argument("--kind", choices=("auto", "coffee", "tea"), default="auto")
    catalog_list = catalog_sub.add_parser("list", help="list private catalog entries")
    catalog_list.add_argument("--kind", choices=("all", "coffee", "tea"), default="all")
    catalog_list.add_argument("--origin")
    catalog_list.add_argument("--query")
    catalog_list.add_argument("--executable", action="store_true")
    catalog_list.add_argument("--slot-compatible", action="store_true")
    catalog_show = catalog_sub.add_parser("show", help="show one normalised catalog entry")
    catalog_show.add_argument("identifier")
    catalog_export = catalog_sub.add_parser(
        "export", help="export one executable entry as guarded YAML"
    )
    catalog_export.add_argument("identifier")
    catalog_export.add_argument("output")
    catalog_export.add_argument("--overwrite", action="store_true")
    catalog_sync = catalog_sub.add_parser(
        "sync",
        help="read the user's own account-visible xBloom catalog using an explicit app form",
    )
    catalog_sync.add_argument("--config")
    catalog_sync.add_argument(
        "--include",
        action="append",
        choices=(
            "coffee",
            "tea",
            "created",
            "product",
            "shared",
            "easy",
            "easy-default",
        ),
        default=None,
        help="repeat to select targets; defaults to every account recipe category",
    )
    catalog_sync.add_argument("--timeout", type=float, default=20.0)
    catalog_login_sync = catalog_sub.add_parser(
        "login-sync",
        help="ephemerally login and read official, created, product, and shared recipes",
    )
    catalog_login_sync.add_argument(
        "--email",
        help="account email; may instead use XBLOOM_ACCOUNT_EMAIL",
    )
    catalog_login_sync.add_argument(
        "--region",
        choices=("international", "china"),
        required=True,
        help="account tenant; APK defaults first-login Simplified Chinese users to china",
    )
    catalog_login_sync.add_argument(
        "--language",
        choices=("en", "zh-cn"),
        default="en",
        help="catalog response language",
    )
    catalog_login_sync.add_argument(
        "--include",
        action="append",
        choices=("coffee", "tea", "created", "product", "shared"),
        default=None,
        help="repeat to select targets; defaults to every account recipe category",
    )
    catalog_login_sync.add_argument("--timeout", type=float, default=20.0)
    catalog_push = catalog_sub.add_parser(
        "push",
        help="preview an add-only local-to-account recipe sync; --apply writes remotely",
    )
    catalog_push.add_argument("recipe", help="guarded local coffee or tea YAML/JSON")
    catalog_push.add_argument(
        "--email",
        help="account email for --apply; may instead use XBLOOM_ACCOUNT_EMAIL",
    )
    catalog_push.add_argument(
        "--region",
        choices=("international", "china"),
        required=True,
        help="account tenant; preview uses it only as explicit operator context",
    )
    catalog_push.add_argument(
        "--language",
        choices=("en", "zh-cn"),
        default="en",
    )
    catalog_push.add_argument(
        "--apply",
        action="store_true",
        help="perform the remote idempotent add after the preview has been reviewed",
    )
    catalog_push.add_argument(
        "--confirm-write",
        default="",
        help="with --apply, must be exactly: own-account-cloud-recipe",
    )
    catalog_push.add_argument("--timeout", type=float, default=20.0)
    catalog_delete = catalog_sub.add_parser(
        "delete",
        help="preview delete of one created cloud recipe by tableId/catalog id; --apply writes remotely",
    )
    catalog_delete.add_argument(
        "--table-id",
        type=int,
        default=None,
        help="remote created-recipe tableId from the account catalog",
    )
    catalog_delete.add_argument(
        "--id",
        dest="identifier",
        default=None,
        help="local catalog id/name used only to resolve a remote tableId for preview",
    )
    catalog_delete.add_argument(
        "--email",
        help="account email for --apply; may instead use XBLOOM_ACCOUNT_EMAIL",
    )
    catalog_delete.add_argument(
        "--region",
        choices=("international", "china"),
        required=True,
    )
    catalog_delete.add_argument(
        "--language",
        choices=("en", "zh-cn"),
        default="en",
    )
    catalog_delete.add_argument(
        "--apply",
        action="store_true",
        help="perform the remote delete after the preview has been reviewed",
    )
    catalog_delete.add_argument(
        "--confirm-delete",
        default="",
        help="with --apply, must be exactly: own-account-cloud-recipe-delete",
    )
    catalog_delete.add_argument("--timeout", type=float, default=20.0)
    catalog_history_sync = catalog_sub.add_parser(
        "history-sync",
        help="import App brew-history records into the local journal via ephemeral login",
    )
    catalog_history_sync.add_argument(
        "--email",
        help="account email; may instead use XBLOOM_ACCOUNT_EMAIL",
    )
    catalog_history_sync.add_argument(
        "--region",
        choices=("international", "china"),
        required=True,
    )
    catalog_history_sync.add_argument(
        "--language",
        choices=("en", "zh-cn"),
        default="en",
    )
    catalog_history_sync.add_argument("--keyword", default="")
    catalog_history_sync.add_argument(
        "--have-pod",
        type=int,
        choices=(0, 1),
        default=None,
        help="optional App filter: 0=non-pod, 1=pod",
    )
    catalog_history_sync.add_argument("--timeout", type=float, default=20.0)
    history = sub.add_parser(
        "history",
        help="inspect the local brew journal or attach a tasting note",
    )
    history_sub = history.add_subparsers(dest="history_action", required=True)
    history_sub.add_parser("status", help="show journal path and counts")
    history_list = history_sub.add_parser("list", help="list recent journal events")
    history_list.add_argument("--limit", type=int, default=20)
    history_list.add_argument("--source", choices=("local-skill", "app-cloud"))
    history_list.add_argument(
        "--outcome",
        choices=(
            "loaded",
            "started",
            "completed",
            "completion_unconfirmed",
            "cancelled",
            "failed",
            "imported",
        ),
    )
    history_list.add_argument("--query")
    history_list.add_argument("--recipe-sha256")
    history_note = history_sub.add_parser(
        "note", help="append a tasting/operator note linked to an existing event"
    )
    history_note.add_argument("event_id")
    history_note.add_argument("note")
    load = sub.add_parser("load", help="load and arm a recipe; never starts brewing")
    load.add_argument("recipe")
    monitor = sub.add_parser(
        "monitor",
        help="observe bridge status/events only (never connects or cancels)",
    )
    monitor.add_argument("--duration", type=float, default=300.0)
    monitor.add_argument(
        "--progress-interval",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="minimum seconds between aggregated weight updates (0.1-60)",
    )
    monitor.add_argument(
        "--workflow-id",
        default=None,
        help="observe a specific durable workflow (default: active)",
    )
    scale = sub.add_parser(
        "scale",
        help="standalone electronic scale; entry automatically zeros the current load",
    )
    scale.add_argument("--duration", type=float, default=30.0)
    scale.add_argument("--interval", type=float, default=0.25, help="minimum seconds between JSON readings")
    scale.add_argument(
        "--tare",
        action="store_true",
        help="send an additional tare after the firmware's mandatory entry auto-zero",
    )
    grind = sub.add_parser("grind", help="explicitly gated standalone grinder")
    grind.add_argument("--size", type=int, required=True, help="grind setting 1-80")
    grind.add_argument("--rpm", type=int, default=100, help="60-120 RPM")
    grind.add_argument("--seconds", type=float, required=True, help="0.1-30 seconds")
    grind.add_argument("--confirm-ready", default="")
    water = sub.add_parser("water", help="explicitly gated temperature/volume water dispense")
    water.add_argument("--volume", type=float, required=True, help="20-360 ml")
    water.add_argument(
        "--temp",
        type=parse_water_temperature,
        required=True,
        help="RT (room-temperature pass-through) or 40-98 C",
    )
    water.add_argument("--flow", type=float, default=3.5, help="3.0-3.5 ml/s")
    water.add_argument(
        "--pattern", choices=("center", "spiral", "circular", "ring"), default="center"
    )
    water.add_argument(
        "--water-source",
        choices=("auto", "tank", "tap"),
        default="auto",
        help="auto uses the machine's current setting; otherwise select tank or tap",
    )
    water.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "observation bound only (5-600 s): poll the returned workflow until "
            "terminal or bound; never cancel/release the daemon. Default: "
            "core safety_timeout_s + 30 s (capped 5-600)"
        ),
    )
    water.add_argument("--confirm-ready", default="")
    tea_validate = sub.add_parser("tea-validate", help="strictly validate an Omni Tea Brewer recipe")
    tea_validate.add_argument("recipe")
    tea_load = sub.add_parser("tea-load", help="upload a tea recipe; never starts it")
    tea_load.add_argument("recipe")
    tea_start = sub.add_parser("tea-start", help="explicitly gated tea recipe execution")
    tea_start.add_argument("recipe")
    tea_start.add_argument("--confirm-ready", default="")
    tea_start.add_argument("--duration", type=float, default=600.0)
    tea_start.add_argument(
        "--progress-interval",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="minimum seconds between aggregated weight updates (0.1-60)",
    )
    tea_brew = sub.add_parser(
        "tea-brew", help="explicitly load and start one tea recipe in a single connection"
    )
    tea_brew.add_argument("recipe")
    tea_brew.add_argument("--confirm-ready", default="")
    tea_brew.add_argument("--duration", type=float, default=600.0)
    tea_brew.add_argument(
        "--progress-interval", type=float, default=DEFAULT_PROGRESS_INTERVAL
    )
    cancel_p = sub.add_parser("cancel", help="cancel/exit an armed or running brew")
    cancel_p.add_argument(
        "--workflow-id",
        default=None,
        help="durable workflow to cancel (default: active workflow)",
    )
    cancel_p.add_argument(
        "--emergency",
        action="store_true",
        help="explicit emergency stop (required when no workflow_id can be resolved)",
    )
    start = sub.add_parser("start", help="explicitly gated remote start")
    start.add_argument("recipe")
    start.add_argument("--confirm-ready", default="")
    start.add_argument("--duration", type=float, default=300.0)
    start.add_argument(
        "--progress-interval",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="observation bound only (0.1-60 progress interval; not recipe expiry)",
    )
    start.add_argument(
        "--workflow-id",
        default=None,
        help="durable workflow from load (default: active coffee loaded workflow)",
    )
    tea_start.add_argument(
        "--workflow-id",
        default=None,
        help="durable workflow from tea-load (default: active tea loaded workflow)",
    )
    slots = sub.add_parser("save-slots", help="write guarded recipes to A/B/C; never brews")
    slots.add_argument("recipes", nargs=3, metavar="RECIPE")
    slots.add_argument(
        "--scale",
        nargs=3,
        choices=("on", "off"),
        default=("on", "on", "on"),
        metavar=("A", "B", "C"),
        help="per-slot on-brew scale behavior in A/B/C order (default: on on on)",
    )
    bridge = sub.add_parser(
        "bridge",
        help="manage the local long-lived BLE owner and interactive controls",
    )
    bridge_sub = bridge.add_subparsers(dest="bridge_action", required=True)
    bridge_sub.add_parser("start", help="start the local daemon; does not connect or actuate")
    bridge_sub.add_parser("status", help="read connection, activity, and telemetry snapshot")
    bridge_stop = bridge_sub.add_parser("stop", help="stop an idle daemon")
    bridge_sub.add_parser(
        "restart-if-idle",
        help="restart only when idle with no recovery records; otherwise report pending",
    )
    bridge_stop.add_argument(
        "--force",
        action="store_true",
        help="first stop/cancel a bridge-owned activity, then shut down",
    )
    bridge_sub.add_parser("serve", help="internal foreground bridge service")
    bridge_sub.add_parser("connect", help="connect and hold an app-style BLE session")
    bridge_sub.add_parser("disconnect", help="disconnect an idle bridge")
    bridge_events = bridge_sub.add_parser("events", help="poll control-grade telemetry events")
    bridge_events.add_argument("--since", type=int, default=0)
    bridge_sub.add_parser("settings", help="read persistent machine settings")
    bridge_set_settings = bridge_sub.add_parser(
        "set-settings", help="gated persistent settings with readback and rollback"
    )
    bridge_set_settings.add_argument("--weight-unit", choices=("ml", "g", "oz"))
    bridge_set_settings.add_argument("--temperature-unit", choices=("C", "F"))
    bridge_set_settings.add_argument("--water-source", choices=("tank", "tap"))
    bridge_set_settings.add_argument("--display", choices=("low", "medium", "high"))
    bridge_set_settings.add_argument("--confirm-write", default="")
    bridge_sub.add_parser("advanced", help="read mechanical tuning values")
    bridge_set_advanced = bridge_sub.add_parser(
        "set-advanced", help="gated mechanical tuning with readback and rollback"
    )
    bridge_set_advanced.add_argument(
        "--pour-radius-level", type=int, choices=range(1, 6)
    )
    bridge_set_advanced.add_argument(
        "--vibration-level", type=int, choices=range(1, 7)
    )
    bridge_set_advanced.add_argument("--confirm-write", default="")
    bridge_load = bridge_sub.add_parser(
        "coffee-load", help="load and arm coffee through the persistent connection"
    )
    bridge_load.add_argument("recipe")
    bridge_coffee_start = bridge_sub.add_parser(
        "coffee-start", help="explicitly gated start of the bridge-loaded coffee recipe"
    )
    bridge_coffee_start.add_argument("--confirm-ready", default="")
    bridge_coffee_start.add_argument("--timeout", type=float, default=60.0)
    bridge_tea_load = bridge_sub.add_parser(
        "tea-load", help="upload a tea recipe through the persistent connection"
    )
    bridge_tea_load.add_argument("recipe")
    bridge_tea_start = bridge_sub.add_parser(
        "tea-start", help="explicitly gated start of the bridge-loaded tea recipe"
    )
    bridge_tea_start.add_argument("--confirm-ready", default="")
    bridge_tea_start.add_argument("--timeout", type=float, default=60.0)
    bridge_scale = bridge_sub.add_parser(
        "scale-start", help="start a non-blocking standalone scale session"
    )
    bridge_scale.add_argument("--duration", type=float, default=30.0)
    bridge_scale.add_argument(
        "--tare",
        action="store_true",
        help="additional tare after mandatory scale-entry auto-zero",
    )
    bridge_sub.add_parser("scale-tare", help="re-tare a running scale session")
    bridge_slots = bridge_sub.add_parser(
        "save-slots", help="write all three guarded A/B/C recipes; never brews"
    )
    bridge_slots.add_argument("recipes", nargs=3, metavar="RECIPE")
    bridge_slots.add_argument(
        "--scale",
        nargs=3,
        choices=("on", "off"),
        default=("on", "on", "on"),
        metavar=("A", "B", "C"),
        help="per-slot on-brew scale behavior in A/B/C order (default: on on on)",
    )
    bridge_grinder = bridge_sub.add_parser(
        "grinder-start", help="start a timed interactive FreeSolo grinder session"
    )
    bridge_grinder.add_argument("--size", type=int, required=True)
    bridge_grinder.add_argument("--rpm", type=int, default=100)
    bridge_grinder.add_argument("--seconds", type=float, required=True)
    bridge_grinder.add_argument("--confirm-ready", default="")
    bridge_water = bridge_sub.add_parser(
        "water-start", help="start bounded interactive FreeSolo water"
    )
    bridge_water.add_argument("--volume", type=float, required=True)
    bridge_water.add_argument("--temp", type=parse_water_temperature, required=True)
    bridge_water.add_argument("--flow", type=float, default=3.5)
    bridge_water.add_argument(
        "--pattern", choices=("center", "spiral", "circular", "ring"), default="center"
    )
    bridge_water.add_argument(
        "--water-source", choices=("auto", "tank", "tap"), default="auto"
    )
    bridge_water.add_argument("--confirm-ready", default="")
    bridge_sub.add_parser("pause", help="pause bridge-owned coffee/grinder/water")
    bridge_sub.add_parser("resume", help="resume the bridge-owned paused activity")
    bridge_cancel = bridge_sub.add_parser(
        "cancel",
        help="stop/cancel the bridge-owned coffee/tea/scale/grinder/water activity",
    )
    bridge_cancel.add_argument(
        "--workflow-id",
        default=None,
        help="durable workflow to cancel (default: active workflow)",
    )
    bridge_cancel.add_argument(
        "--emergency",
        action="store_true",
        help="explicit emergency stop (required when no workflow_id can be resolved)",
    )
    bridge_temp = bridge_sub.add_parser(
        "water-temperature",
        help="APK-verified FreeSolo live temperature command; outlet effect is not measured",
    )
    bridge_temp.add_argument("--temp", type=parse_water_temperature, required=True)
    bridge_temp.add_argument("--confirm-live-adjust", default="")
    bridge_pattern = bridge_sub.add_parser(
        "water-pattern",
        help="hardware-verified FreeSolo live pattern change",
    )
    bridge_pattern.add_argument(
        "--pattern", choices=("center", "spiral", "circular", "ring"), required=True
    )
    bridge_pattern.add_argument("--confirm-live-adjust", default="")
    state = sub.add_parser(
        "state",
        help=(
            "explicit state.db migration/status/backup; does not auto-migrate on "
            "daemon start; SQLite active for workflow/history/idempotency, catalog pending"
        ),
    )
    state_sub = state.add_subparsers(dest="state_action", required=True)
    state_sub.add_parser(
        "status",
        help=(
            "migration receipt + runtime source-of-truth "
            "(SQLite workflow/history/idempotency; catalog still JSON)"
        ),
    )
    state_migrate = state_sub.add_parser(
        "migrate",
        help="idempotent backup+import of legacy JSON/JSONL into state.db",
    )
    state_migrate.add_argument(
        "--force",
        action="store_true",
        help="re-run import even if a migration receipt exists",
    )
    state_migrate.add_argument(
        "--backup-root",
        default=None,
        help="directory for the pre-migration backup tree",
    )
    state_backup = state_sub.add_parser(
        "backup",
        help="online SQLite backup of state.db (does not migrate or cut over runtime)",
    )
    state_backup.add_argument(
        "--destination",
        default=None,
        help="optional destination .db path (must not already exist)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    reexec_in_local_runtime()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "catalog":
            return cmd_catalog(args)
        if args.command == "history":
            return cmd_history(args)
        require_runtime()
        if args.command == "state":
            return cmd_state(args)
        if args.command == "bridge":
            return cmd_bridge(args)
        # All hardware commands go through the typed bridge client (A9).
        # Passive discovery is the only direct BLE path.
        if args.command == "scan":
            return asyncio.run(async_scan(args))
        if args.command == "probe":
            return asyncio.run(async_probe(args))
        if args.command == "settings":
            return asyncio.run(async_settings(args))
        if args.command == "set-settings":
            return asyncio.run(async_set_settings(args))
        if args.command == "advanced":
            return asyncio.run(async_advanced(args))
        if args.command == "set-advanced":
            return asyncio.run(async_set_advanced(args))
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "load":
            return asyncio.run(async_load(args))
        if args.command == "monitor":
            return asyncio.run(async_monitor(args))
        if args.command == "scale":
            return asyncio.run(async_scale(args))
        if args.command == "grind":
            return asyncio.run(async_grind(args))
        if args.command == "water":
            return asyncio.run(async_water(args))
        if args.command == "tea-validate":
            return cmd_tea_validate(args)
        if args.command == "tea-load":
            return asyncio.run(async_tea_load(args))
        if args.command == "tea-start":
            return asyncio.run(async_tea_start(args))
        if args.command == "tea-brew":
            return asyncio.run(async_tea_brew(args))
        if args.command == "cancel":
            return asyncio.run(async_cancel(args))
        if args.command == "start":
            return asyncio.run(async_start(args))
        if args.command == "save-slots":
            return asyncio.run(async_save_slots(args))
        raise RuntimeError(f"unknown command {args.command}")
    except KeyboardInterrupt:
        emit({"command": args.command, "error": "interrupted"})
        return 130
    except Exception as exc:
        payload: dict[str, Any] = {
            "command": args.command,
            "error": str(exc),
            "type": type(exc).__name__,
        }
        # Preserve stable BridgeError.category for Skill branching
        # (e.g. device_busy_external / recovery classes).
        category = getattr(exc, "category", None)
        if category:
            payload["category"] = str(category)
        emit(payload)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

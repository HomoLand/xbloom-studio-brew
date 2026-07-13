"""Guarded xBloom Studio CLI for Agent Skills.

Common flow: doctor -> scan -> probe -> validate -> load -> monitor/cancel.
Physical actions and experimental live adjustment use independent owner gates.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any

from xbloom_paths import (
    legacy_runtime_python,
    preferred_runtime_python,
    runtime_python_path,
    skill_runtime_dir,
    skill_state_dir,
)


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = skill_state_dir()
STATE_FILE = STATE_DIR / "armed-state.json"
TEA_STATE_FILE = STATE_DIR / "tea-loaded-state.json"
GRINDER_STATE_FILE = STATE_DIR / "grinder-rest-state.json"
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
ARM_MAX_AGE_SECONDS = 300
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
    if already_local or os.environ.get("XBLOOM_SKILL_REEXEC") == "1":
        return
    env = dict(os.environ)
    env["XBLOOM_SKILL_REEXEC"] = "1"
    raise SystemExit(subprocess.call([str(target), __file__, *sys.argv[1:]], env=env, cwd=ROOT))


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


async def resolve_address(explicit: str | None, timeout: float) -> tuple[str, str]:
    from xbloom_ble.client import scan

    address = explicit or os.environ.get("XBLOOM_ADDRESS")
    if address:
        return address, "configured"
    devices = await scan(timeout=timeout)
    if len(devices) != 1:
        raise RuntimeError(f"expected exactly one nearby xBloom; found {len(devices)}")
    device = devices[0]
    return device.address, getattr(device, "name", None) or "xBloom"


def loaded_workflow_records() -> list[tuple[Path, dict[str, Any]]]:
    return [
        (path, state_read(path))
        for path in (STATE_FILE, TEA_STATE_FILE)
        if path.exists()
    ]


async def resolve_control_address(explicit: str | None, timeout: float) -> tuple[str, str]:
    """Resolve monitor/cancel against a loaded workflow before scanning.

    A state record is the authoritative machine binding after load/start. Reusing it
    avoids scanning during an armed or running operation and makes recovery reliable
    when several xBloom machines are nearby.
    """
    records = loaded_workflow_records()
    if not records:
        return await resolve_address(explicit, timeout)

    addresses = {str(record.get("address") or "") for _path, record in records}
    if "" in addresses:
        raise RuntimeError("loaded workflow state has no machine address; inspect before recovery")
    if len(addresses) != 1:
        raise RuntimeError("loaded workflow records refer to different machines; inspect before recovery")
    recorded_address = next(iter(addresses))
    configured_address = explicit or os.environ.get("XBLOOM_ADDRESS")
    if configured_address and configured_address.casefold() != recorded_address.casefold():
        raise RuntimeError("requested machine differs from the loaded workflow machine")
    machine = next(
        (
            str(record.get("machine"))
            for _path, record in records
            if record.get("machine")
        ),
        "xBloom",
    )
    return recorded_address, machine


def redact_machine_info(machine_info: dict[str, object]) -> dict[str, object]:
    """Return the diagnostic subset safe to place in normal Agent output."""
    return {
        key: value for key, value in machine_info.items() if key != "serial_number"
    }


async def inspect_machine(address: str, *, duration: float = 4.0) -> dict[str, Any]:
    """Read the vendor service, firmware, and state without sending brew control."""
    from bleak import BleakClient
    from xbloom_ble.client import CHAR_COMMAND, CHAR_STATUS, SERVICE_UUID
    from xbloom_ble.protocol import build_session_start, build_status_query
    from xbloom_ble.telemetry import parse_notification

    seen_types: set[int] = set()
    states: list[str] = []
    firmware: set[str] = set()
    machine_info: dict[str, object] = {}

    def on_status(_sender: object, data: bytearray) -> None:
        raw = bytes(data)
        if len(raw) > 3:
            seen_types.add(raw[3])
        firmware.update(x.decode("ascii", errors="replace") for x in FIRMWARE_RE.findall(raw))
        event = parse_notification(raw)
        if event is not None:
            if event.state is not None:
                states.append(event.state_name)
            if event.machine_info:
                machine_info.update(event.machine_info)
                value = event.machine_info.get("firmware")
                if isinstance(value, str) and value:
                    firmware.add(value)

    session = build_session_start()
    status = build_status_query()
    if {session[3], status[3]} != {0xA4, 0x56}:
        raise RuntimeError("probe opcode invariant failed")

    async with BleakClient(address) as client:
        if SERVICE_UUID.lower() not in {service.uuid.lower() for service in client.services}:
            raise RuntimeError("xBloom vendor service is missing")
        await client.start_notify(CHAR_STATUS, on_status)
        await client.write_gatt_char(CHAR_COMMAND, session, response=False)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(CHAR_COMMAND, status, response=False)
        await asyncio.sleep(duration)
        await client.stop_notify(CHAR_STATUS)

    # A serial number is useful internally for app account/device binding, but
    # probe output is routinely copied into Agent transcripts and bug reports.
    # Keep the useful read-only settings while deliberately redacting identity.
    public_machine_info = redact_machine_info(machine_info)
    return {
        "vendor_service": True,
        "firmware": sorted(firmware),
        "states": list(dict.fromkeys(states)),
        "notification_types": [f"0x{x:02x}" for x in sorted(seen_types)],
        "machine_info": public_machine_info or None,
        "brew_control_sent": False,
    }


def require_write_preflight(report: dict[str, Any]) -> str:
    active = ACTIVE_STATES & set(report.get("states", []))
    if active:
        raise RuntimeError(f"machine is not idle ({', '.join(sorted(active))}); cancel first")
    firmware = set(report.get("firmware", []))
    if firmware and firmware <= SUPPORTED_FIRMWARE:
        return sorted(firmware)[0]
    if os.environ.get(UNTESTED_FIRMWARE_ENV) == UNTESTED_FIRMWARE_SENTINEL:
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
        "vendored_protocol": (ROOT / "scripts" / "xbloom_ble" / "protocol.py").exists(),
        "capabilities": {
            "coffee_recipe": True,
            "coffee_bypass": True,
            "coffee_temperature_modes": ["RT", "40-95 C", "BP"],
            "tea_recipe": (ROOT / "scripts" / "xbloom_ble" / "tea.py").exists(),
            "scale": True,
            "grinder": True,
            "temperature_water": True,
            "temperature_water_modes": ["RT", "40-98 C"],
            "water_sources": ["tank", "tap"],
            "water_source_semantics": {"tank": "reservoir", "tap": "direct_feed"},
            "machine_info": True,
            "persistent_bridge": (
                ROOT / "scripts" / "xbloom_ble" / "bridge.py"
            ).exists(),
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
            "hot_water": os.environ.get(REMOTE_START_ENV) == REMOTE_START_SENTINEL,
            "grinder": os.environ.get(REMOTE_GRINDER_ENV) == REMOTE_GRINDER_SENTINEL,
            "live_adjust_unverified": (
                os.environ.get(LIVE_ADJUST_ENV) == LIVE_ADJUST_SENTINEL
            ),
            "persistent_settings": (
                os.environ.get(SETTINGS_WRITE_ENV) == SETTINGS_WRITE_SENTINEL
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
    if STATE_FILE.exists() or TEA_STATE_FILE.exists():
        raise RuntimeError("a loaded-recipe record exists; use monitor or cancel, not probe")
    address, name = await resolve_address(args.address, args.scan_timeout)
    report = await inspect_machine(address)
    emit({"command": "probe", "machine": name, **report})
    return 0


def require_settings_write_gate(confirmation: str, expected: str) -> None:
    if os.environ.get(SETTINGS_WRITE_ENV) != SETTINGS_WRITE_SENTINEL:
        raise RuntimeError(
            f"persistent machine writes disabled; administrator must set "
            f"{SETTINGS_WRITE_ENV}={SETTINGS_WRITE_SENTINEL}"
        )
    if confirmation != expected:
        raise RuntimeError(f"--confirm-write must equal {expected}")


async def _ephemeral_bridge_rpc(
    address: str, method: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Use the same state machine as the daemon for a one-shot connection."""

    from xbloom_ble.bridge import BridgeCore

    core = BridgeCore(default_address=address, state_dir=STATE_DIR)
    try:
        return await core.rpc(method, params)
    finally:
        await core.shutdown()


async def async_settings(args: argparse.Namespace) -> int:
    """Read persistent user settings without changing the machine."""

    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    result = await _ephemeral_bridge_rpc(address, "settings.read")
    emit({"command": "settings", "machine": name, **result})
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
    address, name = await resolve_address(args.address, args.scan_timeout)
    result = await _ephemeral_bridge_rpc(
        address,
        "settings.write",
        {**requested, "confirmation": args.confirm_write},
    )
    emit(
        {
            "command": "set-settings",
            "machine": name,
            **result,
        }
    )
    return 0


async def async_advanced(args: argparse.Namespace) -> int:
    """Read APK-defined mechanical tuning values."""

    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    result = await _ephemeral_bridge_rpc(address, "advanced.read")
    emit({"command": "advanced", "machine": name, **result})
    return 0


async def async_set_advanced(args: argparse.Namespace) -> int:
    """Write UI-level mechanical tuning with exact readback and rollback."""

    require_settings_write_gate(args.confirm_write, ADVANCED_CONFIRM_SENTINEL)
    if args.pour_radius_level is None and args.vibration_level is None:
        raise RuntimeError("set-advanced needs at least one level option")
    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    result = await _ephemeral_bridge_rpc(
        address,
        "advanced.write",
        {
            "pour_radius_level": args.pour_radius_level,
            "vibration_level": args.vibration_level,
            "confirmation": args.confirm_write,
        },
    )
    emit(
        {
            "command": "set-advanced",
            "machine": name,
            **result,
        }
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path, _recipe, summary = load_recipe(args.recipe)
    emit({"command": "validate", "ok": True, "path": str(path), **summary})
    return 0


async def async_load(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    path, recipe, summary = load_recipe(args.recipe)
    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    async with XBloomClient(address) as client:
        armed = await client.load_recipe(recipe)
    if armed.state_name != "armed":
        raise RuntimeError(f"machine did not arm; state={armed.state_name}")
    state_write(
        {
            "address": address,
            "machine": name,
            "recipe_path": str(path),
            "recipe_sha256": summary["recipe_sha256"],
            "loaded_at": time.time(),
            "status": "armed",
            "firmware": firmware,
            "target_dispensed_water_ml": summary["target_dispensed_water_ml"],
        }
    )
    emit(
        {
            "command": "load",
            "status": "armed",
            "machine": name,
            "firmware": firmware,
            "remote_start_sent": False,
            **summary,
        }
    )
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


async def async_monitor(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    if not 0.1 <= float(args.duration) <= 3600:
        raise RuntimeError("monitor --duration must be 0.1-3600 seconds")
    if not 0.1 <= float(args.progress_interval) <= 60:
        raise RuntimeError("monitor --progress-interval must be 0.1-60 seconds")
    workflow_records = loaded_workflow_records()
    if len(workflow_records) > 1:
        raise RuntimeError("multiple loaded workflow records exist; run cancel before monitoring")
    active_already = any(
        record.get("status") in {"running", "completion_unconfirmed"}
        for _path, record in workflow_records
    )
    address, name = await resolve_control_address(args.address, args.scan_timeout)
    emit(
        {
            "command": "monitor",
            "status": "listening",
            "machine": name,
            "progress_interval_s": args.progress_interval,
        }
    )
    async with XBloomClient(address) as client:
        result = await monitor_client(
            client,
            args.duration,
            progress_interval=args.progress_interval,
            active_already=active_already,
        )
    state_records_cleared = 0
    if result.terminal_confirmed:
        for path, _record in workflow_records:
            state_clear(path)
            state_records_cleared += 1
    emit(
        {
            "command": "monitor",
            "status": (
                result.terminal_state if result.terminal_confirmed else "duration_elapsed"
            ),
            "state_records_cleared": state_records_cleared,
            **result.summary(),
            **(
                volume_comparison(workflow_records[0][1], result)
                if len(workflow_records) == 1
                else {}
            ),
        }
    )
    return 0


async def async_scale(args: argparse.Namespace) -> int:
    """Use the Studio as a standalone electronic scale; no motor or water."""
    from xbloom_ble.client import XBloomClient

    if not 0.05 <= float(args.interval) <= 10.0:
        raise RuntimeError("scale --interval must be 0.05-10 seconds")
    if not 0.1 <= float(args.duration) <= 3600:
        raise RuntimeError("scale --duration must be 0.1-3600 seconds")
    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    emit(
        {
            "command": "scale",
            "status": "entering",
            "machine": name,
            "firmware": firmware,
            "entry_auto_zero": True,
            "extra_tare_requested": bool(args.tare),
        }
    )
    last_emit = 0.0

    def on_weight(event: Any) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if now - last_emit < args.interval:
            return
        last_emit = now
        emit(
            {
                "command": "scale-reading",
                "time": round(time.time(), 3),
                "grams": event.scale_g,
            }
        )

    def on_ready() -> None:
        emit(
            {
                "command": "scale",
                "status": "ready",
                "baseline_zeroed": True,
                "extra_tare_sent": bool(args.tare),
                "instruction": (
                    "place-object-now-for-absolute-weight-or-add-contents-now-for-net-weight"
                ),
            }
        )

    async with XBloomClient(address) as client:
        await client.stream_scale(
            on_weight,
            duration=args.duration,
            tare=args.tare,
            on_ready=on_ready,
        )
    emit({"command": "scale", "status": "exited", "machine": name})
    return 0


async def async_grind(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    if os.environ.get(REMOTE_GRINDER_ENV) != REMOTE_GRINDER_SENTINEL:
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
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    reserve_grinder_rest(args.seconds)
    async with XBloomClient(address) as client:
        stop_ack = await client.grind(args.size, args.rpm, seconds=args.seconds)
    emit(
        {
            "command": "grind",
            "status": "stopped",
            "machine": name,
            "firmware": firmware,
            "size": args.size,
            "rpm": args.rpm,
            "seconds": args.seconds,
            "rest_seconds": GRINDER_REST_SECONDS,
            "verified_stop_command": (
                f"0x{stop_ack.command_code:04x}"
                if stop_ack.command_code is not None
                else None
            ),
        }
    )
    return 0


async def async_water(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    if os.environ.get(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
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
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    water_source = args.water_source
    if water_source == "auto":
        info = preflight.get("machine_info") or {}
        water_source = info.get("water_source")
        if water_source not in {"tank", "tap"}:
            raise RuntimeError(
                "could not read the machine water source; pass --water-source tank or tap"
            )
    water_feed = {"tank": 0, "tap": 1}[water_source]
    async with XBloomClient(address) as client:
        event = await client.dispense_water(
            args.volume,
            args.temp,
            flow_ml_s=args.flow,
            pattern=args.pattern,
            water_feed=water_feed,
            timeout=args.timeout,
        )
    emit(
        {
            "command": "water",
            "status": "complete",
            "verified_by_command": (
                f"0x{event.command_code:04x}" if event.command_code is not None else None
            ),
            "machine": name,
            "firmware": firmware,
            "target_dispensed_water_ml": args.volume,
            "dispensed_water_ml": event.dispensed_water_ml,
            "dispensed_vs_target_ml": round(
                float(event.dispensed_water_ml or 0.0) - float(args.volume), 2
            ),
            "cup_delta_g": (event.report_values or {}).get("cup_delta_g"),
            "temp_c": args.temp,
            "temp_setting": (
                "RT" if args.temp == ROOM_TEMPERATURE_C else f"{args.temp} C"
            ),
            "heating_mode": (
                "room-temperature-pass-through"
                if args.temp == ROOM_TEMPERATURE_C
                else "heated-target"
            ),
            "flow_ml_s": args.flow,
            "pattern": args.pattern,
            "water_source": water_source,
        }
    )
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
    from xbloom_ble.client import XBloomClient

    path, recipe, summary = load_tea_recipe(args.recipe)
    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    async with XBloomClient(address) as client:
        ack = await client.load_tea_recipe(recipe)
    state_write(
        {
            "address": address,
            "machine": name,
            "recipe_path": str(path),
            "recipe_sha256": summary["recipe_sha256"],
            "loaded_at": time.time(),
            "status": "tea_loaded",
            "firmware": firmware,
            "target_dispensed_water_ml": summary["programmed_water_ml"],
        },
        TEA_STATE_FILE,
    )
    emit(
        {
            "command": "tea-load",
            "status": "tea_loaded",
            "machine": name,
            "firmware": firmware,
            "verified_by_command": (
                f"0x{ack.command_code:04x}" if ack.command_code is not None else None
            ),
            "remote_start_sent": False,
            **summary,
        }
    )
    return 0


async def async_tea_start(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient
    from xbloom_safety import recipe_sha256

    if os.environ.get(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"hot-water actions disabled; administrator must set "
            f"{REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != TEA_READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {TEA_READY_SENTINEL}")
    if not 1 <= float(args.duration) <= 3600:
        raise RuntimeError("tea-start --duration must be 1-3600 seconds")
    if not 0.1 <= float(args.progress_interval) <= 60:
        raise RuntimeError("tea-start --progress-interval must be 0.1-60 seconds")
    path, _recipe, summary = load_tea_recipe(args.recipe)
    state = state_read(TEA_STATE_FILE)
    age = time.time() - float(state.get("loaded_at", 0))
    if age < 0 or age > ARM_MAX_AGE_SECONDS:
        raise RuntimeError("loaded tea state is older than 5 minutes; load the recipe again")
    if state.get("recipe_sha256") != recipe_sha256(path):
        raise RuntimeError("tea recipe changed since it was loaded")
    if state.get("status") != "tea_loaded":
        raise RuntimeError("tea state record is not loaded; load the recipe again")
    address = str(state.get("address") or "")
    if not address:
        raise RuntimeError("loaded tea state has no machine address")
    if args.address and args.address.casefold() != address.casefold():
        raise RuntimeError("requested machine differs from the loaded tea machine")

    async with XBloomClient(address) as client:
        ack = await client.start_tea()
        state = mark_workflow_started(state, TEA_STATE_FILE, "start_accepted")
        emit(
            {
                "command": "tea-start",
                "status": "start_accepted",
                "verified_by_command": (
                    f"0x{ack.command_code:04x}" if ack.command_code is not None else None
                ),
                "recipe_sha256": summary["recipe_sha256"],
            }
        )
        result = await monitor_client(
            client,
            args.duration,
            progress_interval=args.progress_interval,
            active_already=True,
        )
    state_record_cleared = finalize_workflow_state(state, TEA_STATE_FILE, result)
    output = {
        "command": "tea-start",
        "status": (
            result.terminal_state if result.terminal_confirmed else "completion_unconfirmed"
        ),
        "state_record_cleared": state_record_cleared,
        **result.summary(),
        **volume_comparison(state, result),
    }
    if not result.terminal_confirmed:
        output["next_action"] = "run monitor or cancel; do not assume tea completed"
    emit(output)
    return 0 if result.terminal_confirmed else UNCONFIRMED_COMPLETION_EXIT


async def async_tea_brew(args: argparse.Namespace) -> int:
    """Load then explicitly execute tea in one connected, recoverable workflow."""
    from xbloom_ble.client import XBloomClient

    if os.environ.get(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"hot-water actions disabled; administrator must set "
            f"{REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != TEA_READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {TEA_READY_SENTINEL}")
    if not 1 <= float(args.duration) <= 3600:
        raise RuntimeError("tea-brew --duration must be 1-3600 seconds")
    if not 0.1 <= float(args.progress_interval) <= 60:
        raise RuntimeError("tea-brew --progress-interval must be 0.1-60 seconds")

    path, recipe, summary = load_tea_recipe(args.recipe)
    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    state = {
        "address": address,
        "machine": name,
        "recipe_path": str(path),
        "recipe_sha256": summary["recipe_sha256"],
        "loaded_at": time.time(),
        "status": "tea_loaded",
        "firmware": firmware,
        "target_dispensed_water_ml": summary["programmed_water_ml"],
    }

    async with XBloomClient(address) as client:
        load_ack = await client.load_tea_recipe(recipe)
        state_write(state, TEA_STATE_FILE)
        emit(
            {
                "command": "tea-brew",
                "status": "tea_loaded",
                "machine": name,
                "firmware": firmware,
                "verified_by_command": (
                    f"0x{load_ack.command_code:04x}"
                    if load_ack.command_code is not None
                    else None
                ),
                **summary,
            }
        )
        start_ack = await client.start_tea()
        state = mark_workflow_started(state, TEA_STATE_FILE, "start_accepted")
        emit(
            {
                "command": "tea-brew",
                "status": "start_accepted",
                "verified_by_command": (
                    f"0x{start_ack.command_code:04x}"
                    if start_ack.command_code is not None
                    else None
                ),
            }
        )
        result = await monitor_client(
            client,
            args.duration,
            progress_interval=args.progress_interval,
            active_already=True,
        )

    state_record_cleared = finalize_workflow_state(state, TEA_STATE_FILE, result)
    output = {
        "command": "tea-brew",
        "status": (
            result.terminal_state if result.terminal_confirmed else "completion_unconfirmed"
        ),
        "state_record_cleared": state_record_cleared,
        **result.summary(),
        **volume_comparison(state, result),
    }
    if not result.terminal_confirmed:
        output["next_action"] = "run monitor or cancel; do not assume tea completed"
    emit(output)
    return 0 if result.terminal_confirmed else UNCONFIRMED_COMPLETION_EXIT


async def async_cancel(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    address, name = await resolve_control_address(args.address, args.scan_timeout)
    async with XBloomClient(address) as client:
        await client.cancel_brew()
        if TEA_STATE_FILE.exists():
            await asyncio.sleep(0.2)
            await client.unload_tea_recipe()
        await asyncio.sleep(0.5)
    state_clear()
    state_clear(TEA_STATE_FILE)
    emit(
        {
            "command": "cancel",
            "status": "cancel_sent",
            "machine": name,
            "coffee_state_cleared": True,
            "tea_state_cleared": True,
        }
    )
    return 0


async def async_start(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient
    from xbloom_safety import recipe_sha256

    if os.environ.get(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
        raise RuntimeError(
            f"remote start disabled; administrator must set {REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
        )
    if args.confirm_ready != READY_SENTINEL:
        raise RuntimeError(f"--confirm-ready must equal {READY_SENTINEL}")
    if not 1 <= float(args.duration) <= 3600:
        raise RuntimeError("start --duration must be 1-3600 seconds")
    if not 0.1 <= float(args.progress_interval) <= 60:
        raise RuntimeError("start --progress-interval must be 0.1-60 seconds")
    path, _recipe, summary = load_recipe(args.recipe)
    state = state_read()
    age = time.time() - float(state.get("loaded_at", 0))
    if age < 0 or age > ARM_MAX_AGE_SECONDS:
        raise RuntimeError("armed state is older than 5 minutes; load the recipe again")
    if state.get("recipe_sha256") != recipe_sha256(path):
        raise RuntimeError("recipe changed since it was loaded")
    if state.get("status") != "armed":
        raise RuntimeError("armed-state record is not armed; load the recipe again")
    address = str(state.get("address") or "")
    if not address:
        raise RuntimeError("armed state has no machine address")
    if args.address and args.address.casefold() != address.casefold():
        raise RuntimeError("requested machine differs from the armed machine")

    async with XBloomClient(address) as client:
        event = await client.start()
        state = mark_workflow_started(state, STATE_FILE, event.state_name)
        emit(
            {
                "command": "start",
                "status": event.state_name,
                "verified_by_notification": bool(event.raw),
                "recipe_sha256": summary["recipe_sha256"],
            }
        )
        result = await monitor_client(
            client,
            args.duration,
            progress_interval=args.progress_interval,
            active_already=(bool(event.raw) and event.state_name in ACTIVE_STATES),
        )
    state_record_cleared = finalize_workflow_state(state, STATE_FILE, result)
    output = {
        "command": "start",
        "status": (
            result.terminal_state if result.terminal_confirmed else "completion_unconfirmed"
        ),
        "state_record_cleared": state_record_cleared,
        **result.summary(),
        **volume_comparison(state, result),
    }
    if not result.terminal_confirmed:
        output["next_action"] = "run monitor or cancel; do not assume brew completed"
    emit(output)
    return 0 if result.terminal_confirmed else UNCONFIRMED_COMPLETION_EXIT


async def async_save_slots(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    loaded = [load_recipe(path) for path in args.recipes]
    ensure_no_loaded_workflow()
    address, name = await resolve_address(args.address, args.scan_timeout)
    preflight = await inspect_machine(address)
    firmware = require_write_preflight(preflight)
    async with XBloomClient(address) as client:
        await client.save_slots([item[1] for item in loaded])
    emit(
        {
            "command": "save-slots",
            "status": "saved",
            "machine": name,
            "firmware": firmware,
            "slots": [item[2]["name"] for item in loaded],
            "brew_started": False,
        }
    )
    return 0


DIRECT_BLE_COMMANDS = frozenset(
    {
        "probe",
        "settings",
        "set-settings",
        "advanced",
        "set-advanced",
        "load",
        "monitor",
        "scale",
        "grind",
        "water",
        "tea-load",
        "tea-start",
        "tea-brew",
        "cancel",
        "start",
        "save-slots",
    }
)


def ensure_bridge_not_running(command: str) -> None:
    """Prevent a one-shot client from racing the long-lived BLE owner."""
    if command not in DIRECT_BLE_COMMANDS:
        return
    from xbloom_ble.bridge import bridge_is_running

    if bridge_is_running():
        raise RuntimeError(
            "the local BLE bridge is running and owns Studio access; use `bridge ...` "
            "commands or stop the bridge before using one-shot BLE commands"
        )


def cmd_bridge(args: argparse.Namespace) -> int:
    from xbloom_ble.bridge import (
        BridgeError,
        bridge_call,
        bridge_is_running,
        bridge_record_path,
        serve_bridge,
        start_bridge_daemon,
    )

    action = args.bridge_action
    if action == "serve":
        asyncio.run(serve_bridge(address=args.address))
        return 0
    if action == "start":
        result = start_bridge_daemon(Path(__file__), address=args.address)
    elif action == "status":
        if not bridge_is_running():
            result = {
                "running": False,
                "connected": False,
                "record": str(bridge_record_path()),
            }
        else:
            result = bridge_call("status")
    elif action == "stop":
        if not bridge_is_running():
            result = {"running": False, "status": "already_stopped"}
        else:
            result = bridge_call("shutdown", {"force": bool(args.force)})
    elif action == "connect":
        result = bridge_call(
            "connect",
            {"address": args.address, "scan_timeout": args.scan_timeout},
        )
    elif action == "disconnect":
        result = bridge_call("disconnect")
    elif action == "events":
        result = bridge_call("events", {"since": args.since})
    elif action == "settings":
        result = bridge_call(
            "settings.read",
            {"address": args.address, "scan_timeout": args.scan_timeout},
        )
    elif action == "set-settings":
        result = bridge_call(
            "settings.write",
            {
                "weight_unit": args.weight_unit,
                "temperature_unit": args.temperature_unit,
                "water_source": args.water_source,
                "display": args.display,
                "confirmation": args.confirm_write,
                "address": args.address,
                "scan_timeout": args.scan_timeout,
            },
        )
    elif action == "advanced":
        result = bridge_call(
            "advanced.read",
            {"address": args.address, "scan_timeout": args.scan_timeout},
        )
    elif action == "set-advanced":
        result = bridge_call(
            "advanced.write",
            {
                "pour_radius_level": args.pour_radius_level,
                "vibration_level": args.vibration_level,
                "confirmation": args.confirm_write,
                "address": args.address,
                "scan_timeout": args.scan_timeout,
            },
        )
    elif action == "coffee-load":
        recipe = str(Path(args.recipe).expanduser().resolve(strict=True))
        result = bridge_call(
            "coffee.load",
            {"recipe": recipe, "address": args.address, "scan_timeout": args.scan_timeout},
        )
    elif action == "coffee-start":
        result = bridge_call(
            "coffee.start", {"confirmation": args.confirm_ready}, timeout=args.timeout
        )
    elif action == "tea-load":
        recipe = str(Path(args.recipe).expanduser().resolve(strict=True))
        result = bridge_call(
            "tea.load",
            {"recipe": recipe, "address": args.address, "scan_timeout": args.scan_timeout},
        )
    elif action == "tea-start":
        result = bridge_call(
            "tea.start", {"confirmation": args.confirm_ready}, timeout=args.timeout
        )
    elif action == "scale-start":
        result = bridge_call(
            "scale.start",
            {
                "duration_s": args.duration,
                "tare": bool(args.tare),
                "address": args.address,
                "scan_timeout": args.scan_timeout,
            },
        )
    elif action == "scale-tare":
        result = bridge_call("scale.tare")
    elif action == "save-slots":
        recipes = [
            str(Path(path).expanduser().resolve(strict=True)) for path in args.recipes
        ]
        result = bridge_call(
            "presets.save",
            {
                "recipes": recipes,
                "address": args.address,
                "scan_timeout": args.scan_timeout,
            },
        )
    elif action == "grinder-start":
        result = bridge_call(
            "grinder.start",
            {
                "size": args.size,
                "rpm": args.rpm,
                "seconds": args.seconds,
                "confirmation": args.confirm_ready,
                "address": args.address,
                "scan_timeout": args.scan_timeout,
            },
        )
    elif action == "water-start":
        result = bridge_call(
            "water.start",
            {
                "volume_ml": args.volume,
                "temp_c": args.temp,
                "flow_ml_s": args.flow,
                "pattern": args.pattern,
                "water_source": args.water_source,
                "confirmation": args.confirm_ready,
                "address": args.address,
                "scan_timeout": args.scan_timeout,
            },
        )
    elif action in {"pause", "resume", "cancel"}:
        result = bridge_call(action)
    elif action == "water-temperature":
        result = bridge_call(
            "water.set_temperature",
            {"temp_c": args.temp, "confirmation": args.confirm_live_adjust},
        )
    elif action == "water-pattern":
        result = bridge_call(
            "water.set_pattern",
            {"pattern": args.pattern, "confirmation": args.confirm_live_adjust},
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
    load = sub.add_parser("load", help="load and arm a recipe; never starts brewing")
    load.add_argument("recipe")
    monitor = sub.add_parser("monitor", help="stream status/weights without starting")
    monitor.add_argument("--duration", type=float, default=300.0)
    monitor.add_argument(
        "--progress-interval",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="minimum seconds between aggregated weight updates (0.1-60)",
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
    water.add_argument("--timeout", type=float, default=None, help="completion timeout (5-600 s)")
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
    sub.add_parser("cancel", help="cancel/exit an armed or running brew")
    start = sub.add_parser("start", help="explicitly gated remote start")
    start.add_argument("recipe")
    start.add_argument("--confirm-ready", default="")
    start.add_argument("--duration", type=float, default=300.0)
    start.add_argument(
        "--progress-interval",
        type=float,
        default=DEFAULT_PROGRESS_INTERVAL,
        help="minimum seconds between aggregated weight updates (0.1-60)",
    )
    slots = sub.add_parser("save-slots", help="write guarded recipes to A/B/C; never brews")
    slots.add_argument("recipes", nargs=3, metavar="RECIPE")
    bridge = sub.add_parser(
        "bridge",
        help="manage the local long-lived BLE owner and interactive controls",
    )
    bridge_sub = bridge.add_subparsers(dest="bridge_action", required=True)
    bridge_sub.add_parser("start", help="start the local daemon; does not connect or actuate")
    bridge_sub.add_parser("status", help="read connection, activity, and telemetry snapshot")
    bridge_stop = bridge_sub.add_parser("stop", help="stop an idle daemon")
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
    bridge_sub.add_parser(
        "cancel", help="stop/cancel the bridge-owned coffee/tea/scale/grinder/water activity"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    reexec_in_local_runtime()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        require_runtime()
        if args.command == "bridge":
            return cmd_bridge(args)
        ensure_bridge_not_running(args.command)
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
        emit({"command": args.command, "error": str(exc), "type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

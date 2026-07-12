"""Guarded xBloom Studio CLI for Agent Skills.

Common flow: doctor -> scan -> probe -> validate -> load -> monitor/cancel.
Remote start exists behind two independent opt-ins and is never the default.
"""

from __future__ import annotations

import argparse
import asyncio
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


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path(os.environ.get("XBLOOM_SKILL_STATE_DIR", Path.home() / ".xbloom-studio-brew"))
STATE_FILE = STATE_DIR / "armed-state.json"
TEA_STATE_FILE = STATE_DIR / "tea-loaded-state.json"
GRINDER_STATE_FILE = STATE_DIR / "grinder-rest-state.json"
REMOTE_START_ENV = "XBLOOM_ENABLE_REMOTE_START"
REMOTE_START_SENTINEL = "I_UNDERSTAND_REMOTE_HOT_WATER"
REMOTE_GRINDER_ENV = "XBLOOM_ENABLE_REMOTE_GRINDER"
REMOTE_GRINDER_SENTINEL = "I_UNDERSTAND_REMOTE_GRINDER"
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


def local_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


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
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))


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


def state_write(data: dict[str, Any], path: Path = STATE_FILE) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def state_read(path: Path = STATE_FILE) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"no valid state record at {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"state record at {path} is invalid")
    return data


def state_clear(path: Path = STATE_FILE) -> None:
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


async def inspect_machine(address: str, *, duration: float = 4.0) -> dict[str, Any]:
    """Read the vendor service, firmware, and state without sending brew control."""
    from bleak import BleakClient
    from xbloom_ble.client import CHAR_COMMAND, CHAR_STATUS, SERVICE_UUID
    from xbloom_ble.protocol import build_session_start, build_status_query
    from xbloom_ble.telemetry import parse_notification

    seen_types: set[int] = set()
    states: list[str] = []
    firmware: set[str] = set()

    def on_status(_sender: object, data: bytearray) -> None:
        raw = bytes(data)
        if len(raw) > 3:
            seen_types.add(raw[3])
        firmware.update(x.decode("ascii", errors="replace") for x in FIRMWARE_RE.findall(raw))
        event = parse_notification(raw)
        if event is not None and event.state is not None:
            states.append(event.state_name)

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

    return {
        "vendor_service": True,
        "firmware": sorted(firmware),
        "states": list(dict.fromkeys(states)),
        "notification_types": [f"0x{x:02x}" for x in sorted(seen_types)],
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
    report: dict[str, Any] = {
        "command": "doctor",
        "ok": runtime_ready(),
        "python": sys.version.split()[0],
        "platform": platform.system().lower(),
        "local_runtime": str(local_python()),
        "local_runtime_exists": local_python().exists(),
        "bleak": importlib.util.find_spec("bleak") is not None,
        "pyyaml": importlib.util.find_spec("yaml") is not None,
        "vendored_protocol": (ROOT / "scripts" / "xbloom_ble" / "protocol.py").exists(),
        "capabilities": {
            "coffee_recipe": True,
            "tea_recipe": (ROOT / "scripts" / "xbloom_ble" / "tea.py").exists(),
            "scale": True,
            "grinder": True,
            "temperature_water": True,
        },
        "physical_actions_enabled": {
            "hot_water": os.environ.get(REMOTE_START_ENV) == REMOTE_START_SENTINEL,
            "grinder": os.environ.get(REMOTE_GRINDER_ENV) == REMOTE_GRINDER_SENTINEL,
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


async def monitor_client(client: Any, duration: float) -> None:
    active_states = {0x1F, 0x1E, 0x22, 0x10, 0x23, 0x3B}
    terminal_states = {0x24, 0x41, 0x01}
    saw_active = False

    def on_event(event: Any) -> None:
        nonlocal saw_active
        if event.state in active_states:
            saw_active = True
        data: dict[str, Any] = {
            "command": "telemetry",
            "time": round(time.time(), 3),
            "state": event.state_name,
        }
        if event.water_g is not None:
            data["water_g"] = event.water_g
        if event.coffee_g is not None:
            data["coffee_g"] = event.coffee_g
        if event.scale_g is not None:
            data["scale_g"] = event.scale_g
        emit(data)
        if saw_active and event.state in terminal_states:
            raise _MonitorComplete()

    try:
        await client.stream_telemetry(on_event, duration=duration, stop_on_terminal=False)
    except _MonitorComplete:
        return


async def async_monitor(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    if not 0.1 <= float(args.duration) <= 3600:
        raise RuntimeError("monitor --duration must be 0.1-3600 seconds")
    address, name = await resolve_address(args.address, args.scan_timeout)
    emit({"command": "monitor", "status": "listening", "machine": name})
    async with XBloomClient(address) as client:
        await monitor_client(client, args.duration)
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
    async with XBloomClient(address) as client:
        event = await client.dispense_water(
            args.volume,
            args.temp,
            flow_ml_s=args.flow,
            pattern=args.pattern,
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
            "volume_ml": args.volume,
            "metered_volume_ml": event.water_g,
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
    if args.address and args.address != address:
        raise RuntimeError("requested machine differs from the loaded tea machine")

    async with XBloomClient(address) as client:
        ack = await client.start_tea()
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
        await monitor_client(client, args.duration)
    state_clear(TEA_STATE_FILE)
    return 0


async def async_cancel(args: argparse.Namespace) -> int:
    from xbloom_ble.client import XBloomClient

    address, name = await resolve_address(args.address, args.scan_timeout)
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
    if args.address and args.address != address:
        raise RuntimeError("requested machine differs from the armed machine")

    async with XBloomClient(address) as client:
        event = await client.start()
        emit(
            {
                "command": "start",
                "status": event.state_name,
                "verified_by_notification": bool(event.raw),
                "recipe_sha256": summary["recipe_sha256"],
            }
        )
        await monitor_client(client, args.duration)
    state_clear()
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="BLE address/identifier; defaults to XBLOOM_ADDRESS or scan")
    parser.add_argument("--scan-timeout", type=float, default=8.0)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the local runtime")
    doctor.add_argument("--scan", action="store_true", help="also scan without connecting")
    sub.add_parser("scan", help="discover nearby xBloom machines without writing")
    sub.add_parser("probe", help="safe session/status probe; never use while a recipe is armed")
    validate = sub.add_parser("validate", help="strictly validate a local recipe")
    validate.add_argument("recipe")
    load = sub.add_parser("load", help="load and arm a recipe; never starts brewing")
    load.add_argument("recipe")
    monitor = sub.add_parser("monitor", help="stream status/weights without starting")
    monitor.add_argument("--duration", type=float, default=300.0)
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
    water.add_argument("--pattern", choices=("center", "spiral", "ring"), default="center")
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
    sub.add_parser("cancel", help="cancel/exit an armed or running brew")
    start = sub.add_parser("start", help="explicitly gated remote start")
    start.add_argument("recipe")
    start.add_argument("--confirm-ready", default="")
    start.add_argument("--duration", type=float, default=300.0)
    slots = sub.add_parser("save-slots", help="write guarded recipes to A/B/C; never brews")
    slots.add_argument("recipes", nargs=3, metavar="RECIPE")
    return parser


def main(argv: list[str] | None = None) -> int:
    reexec_in_local_runtime()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        require_runtime()
        if args.command == "scan":
            return asyncio.run(async_scan(args))
        if args.command == "probe":
            return asyncio.run(async_probe(args))
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

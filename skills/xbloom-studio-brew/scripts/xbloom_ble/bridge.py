"""Local long-lived BLE owner for interactive xBloom Studio control.

The bridge listens only on loopback, authenticates every JSON-line request with
a random per-process token, serializes state-changing RPCs, and owns the sole
``XBloomClient`` connection. It is intentionally a local Tool boundary: recipe
design and physical-readiness policy remain in the Agent Skill/CLI.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from typing import Any

from xbloom_paths import (
    environment_copy,
    environment_value,
    skill_state_dir as _shared_skill_state_dir,
)

from .client import XBloomClient, scan
from .protocol import ROOM_TEMPERATURE_C
from .telemetry import StatusEvent


BRIDGE_PROTOCOL_VERSION = 1
BRIDGE_HOST = "127.0.0.1"
BRIDGE_RECORD_NAME = "bridge.json"
BRIDGE_LOG_NAME = "bridge.log"
COFFEE_STATE_NAME = "armed-state.json"
TEA_STATE_NAME = "tea-loaded-state.json"
GRINDER_STATE_NAME = "grinder-rest-state.json"

REMOTE_START_ENV = "XBLOOM_ENABLE_REMOTE_START"
REMOTE_START_SENTINEL = "I_UNDERSTAND_REMOTE_HOT_WATER"
REMOTE_GRINDER_ENV = "XBLOOM_ENABLE_REMOTE_GRINDER"
REMOTE_GRINDER_SENTINEL = "I_UNDERSTAND_REMOTE_GRINDER"
UNTESTED_FIRMWARE_ENV = "XBLOOM_ALLOW_UNTESTED_FIRMWARE"
UNTESTED_FIRMWARE_SENTINEL = "I_ACCEPT_UNTESTED_FIRMWARE"
LIVE_ADJUST_ENV = "XBLOOM_ENABLE_LIVE_ADJUST"
LIVE_ADJUST_SENTINEL = "I_ACCEPT_UNVERIFIED_LIVE_ADJUST"
SETTINGS_WRITE_ENV = "XBLOOM_ENABLE_SETTINGS_WRITE"
SETTINGS_WRITE_SENTINEL = "I_ACCEPT_PERSISTENT_MACHINE_SETTINGS"

READY_SENTINEL = "cup-filter-water-beans"
WATER_READY_SENTINEL = "vessel-water-clear"
GRINDER_READY_SENTINEL = "beans-cup-clear"
TEA_READY_SENTINEL = "tea-brewer-water-cup-clear"
SETTINGS_CONFIRM_SENTINEL = "persistent-machine-settings"
ADVANCED_CONFIRM_SENTINEL = "mechanical-tuning"
SUPPORTED_FIRMWARE = frozenset({"V12.0D.500"})
LIVE_PATTERN_VERIFIED_FIRMWARE = frozenset({"V12.0D.500"})
ACTIVE_MACHINE_STATES = frozenset(
    {"armed", "awaiting_confirm", "starting", "brewing", "saving_slots"}
)
ACTIVE_STATE_BYTES = frozenset({0x1F, 0x1E, 0x22, 0x10, 0x23, 0x3B})
TERMINAL_STATE_BYTES = frozenset({0x24, 0x41, 0x01})
REPORT_BREWER_STOP = 40511
GRINDER_REST_SECONDS = 60
ARM_MAX_AGE_SECONDS = 300


class BridgeError(RuntimeError):
    """Safe, user-facing bridge failure."""


def skill_state_dir() -> Path:
    return _shared_skill_state_dir()


def bridge_record_path() -> Path:
    return skill_state_dir() / BRIDGE_RECORD_NAME


def _atomic_json(path: Path, data: Mapping[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(dict(data), indent=2, sort_keys=True), encoding="utf-8")
    if private and os.name != "nt":
        temp.chmod(0o600)
    temp.replace(path)
    if private and os.name != "nt":
        path.chmod(0o600)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise BridgeError(f"no valid bridge record at {path}") from exc
    if not isinstance(value, dict):
        raise BridgeError(f"bridge record at {path} is invalid")
    return value


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _public_machine_info(info: Mapping[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {}
    return {key: value for key, value in info.items() if key != "serial_number"}


class BridgeCore:
    """State machine and BLE owner, independent of the socket transport."""

    def __init__(
        self,
        *,
        default_address: str | None = None,
        state_dir: Path | None = None,
        client_factory: Callable[[str], Any] = XBloomClient,
        scan_fn: Callable[..., Any] = scan,
        environ: Mapping[str, str] | None = None,
        machine_info_timeout: float = 4.0,
    ) -> None:
        self.default_address = default_address or environment_value("XBLOOM_ADDRESS")
        self.state_dir = Path(state_dir) if state_dir is not None else skill_state_dir()
        self.client_factory = client_factory
        self.scan_fn = scan_fn
        self.environ = environment_copy(environ)
        self.machine_info_timeout = float(machine_info_timeout)

        self.client: Any | None = None
        self.address: str | None = None
        self.machine_name: str | None = None
        self.activity: str | None = None
        self.phase = "disconnected"
        self.machine_state: str | None = None
        self.machine_info: dict[str, Any] = {}
        self.targets: dict[str, Any] = {}
        self.telemetry: dict[str, Any] = {}
        self.last_operation: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._saw_active = False
        self._events: deque[dict[str, Any]] = deque(maxlen=2048)
        self._event_seq = 0
        self._op_lock = asyncio.Lock()
        self._machine_info_ready = asyncio.Event()
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._grinder_timer: asyncio.Task[Any] | None = None
        self._grinder_remaining = 0.0
        self._grinder_started_at: float | None = None
        self._water_timer: asyncio.Task[Any] | None = None
        self._scale_task: asyncio.Task[Any] | None = None
        self._cup_baseline_g: float | None = None

    @property
    def coffee_state_file(self) -> Path:
        return self.state_dir / COFFEE_STATE_NAME

    @property
    def tea_state_file(self) -> Path:
        return self.state_dir / TEA_STATE_NAME

    @property
    def grinder_state_file(self) -> Path:
        return self.state_dir / GRINDER_STATE_NAME

    @property
    def connected(self) -> bool:
        return bool(self.client is not None and self.client.is_connected)

    def _event_dict(self, event: StatusEvent) -> dict[str, Any]:
        self._event_seq += 1
        data: dict[str, Any] = {
            "seq": self._event_seq,
            "time": round(time.time(), 3),
            "state": event.state_name,
        }
        if event.command_code is not None:
            data["command_code"] = event.command_code
        if event.report_name is not None:
            data["report"] = event.report_name
        dispensed = event.dispensed_water_ml
        cup_weight = event.cup_weight_g
        if dispensed is not None:
            data["dispensed_water_ml"] = dispensed
            data["water_ml"] = dispensed  # compatibility through protocol v1
        if cup_weight is not None:
            data["cup_weight_g"] = cup_weight
            data["coffee_g"] = cup_weight  # compatibility through protocol v1
        if event.scale_g is not None:
            data["scale_g"] = event.scale_g
        if event.brewer_pattern is not None:
            data["brewer_pattern"] = event.brewer_pattern
        if event.brewer_temperature_value is not None:
            data["brewer_temperature_value"] = event.brewer_temperature_value
        if event.report_value is not None:
            data["report_value"] = event.report_value
        if event.report_values is not None:
            data["report_values"] = dict(event.report_values)
        if event.is_error:
            data["error_report"] = True
        if event.machine_info:
            data["machine_info"] = _public_machine_info(event.machine_info)
        return data

    def _on_event(self, event: StatusEvent) -> None:
        data = self._event_dict(event)
        self._events.append(data)
        self.telemetry["last_event_at"] = data["time"]
        if event.state is not None:
            self.machine_state = event.state_name
            self.telemetry["machine_state"] = event.state_name
        dispensed = event.dispensed_water_ml
        cup_weight = event.cup_weight_g
        if dispensed is not None:
            self.telemetry["dispensed_water_ml"] = dispensed
            peak = max(
                float(self.telemetry.get("dispensed_water_peak_ml", 0.0)),
                float(dispensed),
            )
            self.telemetry["dispensed_water_peak_ml"] = peak
        if cup_weight is not None:
            value = float(cup_weight)
            self.telemetry["cup_weight_g"] = value
            if self.activity in {"coffee", "water", "tea"}:
                if self._cup_baseline_g is None:
                    self._cup_baseline_g = value
                else:
                    self._cup_baseline_g = min(self._cup_baseline_g, value)
                delta = round(max(0.0, value - self._cup_baseline_g), 2)
                self.telemetry["cup_baseline_g"] = self._cup_baseline_g
                self.telemetry["cup_delta_g"] = delta
                self.telemetry["cup_delta_peak_g"] = max(
                    float(self.telemetry.get("cup_delta_peak_g", 0.0)), delta
                )
        if event.scale_g is not None:
            self.telemetry["scale_g"] = event.scale_g
        if event.report_name is not None:
            self.telemetry["last_report"] = event.report_name
        if event.brewer_pattern is not None:
            self.telemetry["applied_pattern"] = event.brewer_pattern
        if event.brewer_temperature_value is not None:
            self.telemetry["applied_temperature_value"] = event.brewer_temperature_value
        if event.machine_info:
            self.machine_info.update(_public_machine_info(event.machine_info))
            self._machine_info_ready.set()

        if self.activity == "tea":
            if event.report_name == "tea_soaking":
                self.phase = "soaking"
            elif event.report_name == "tea_paused":
                self.phase = "paused"
            elif event.report_name == "tea_restarted":
                self.phase = "running"

        if event.state in ACTIVE_STATE_BYTES:
            self._saw_active = True
        if (
            self.activity == "coffee"
            and self.phase in {"running", "paused", "starting", "control_unconfirmed"}
            and self._saw_active
            and event.state in TERMINAL_STATE_BYTES
        ):
            self._finish_activity(event.state_name)
        if (
            self.activity == "tea"
            and self.phase in {"running", "soaking", "paused", "starting"}
            and self._saw_active
            and event.state in TERMINAL_STATE_BYTES
        ):
            self._finish_activity(event.state_name)

        if (
            self.activity == "water"
            and self.phase
            in {"running", "paused", "starting", "control_unconfirmed", "stop_unconfirmed"}
            and event.command_code == REPORT_BREWER_STOP
            and (self._cleanup_task is None or self._cleanup_task.done())
        ):
            self._cleanup_task = asyncio.create_task(self._finish_natural_water())

    def _finish_activity(self, result: str, **details: Any) -> None:
        previous = self.activity
        if previous in {"coffee", "water", "tea"}:
            target = self.targets.get("target_dispensed_water_ml")
            if target is None:
                target = self.targets.get("volume_ml")
            details.setdefault("target_dispensed_water_ml", target)
            details.setdefault(
                "dispensed_water_ml",
                self.telemetry.get("dispensed_water_peak_ml"),
            )
            details.setdefault("cup_delta_g", self.telemetry.get("cup_delta_peak_g"))
        self.last_operation = {
            "activity": previous,
            "result": result,
            "finished_at": round(time.time(), 3),
            **details,
        }
        if previous == "coffee":
            _unlink(self.coffee_state_file)
        if previous == "tea":
            _unlink(self.tea_state_file)
        if previous == "grinder":
            self._cancel_grinder_timer()
        if previous == "water":
            self._cancel_water_timer()
        self.activity = None
        self.phase = "idle" if self.connected else "disconnected"
        self.targets = {}
        self._saw_active = False
        self._cup_baseline_g = None

    def _reset_liquid_telemetry(self) -> None:
        for key in (
            "dispensed_water_ml",
            "dispensed_water_peak_ml",
            "water_ml",
            "water_peak_ml",
            "cup_weight_g",
            "coffee_g",
            "cup_baseline_g",
            "cup_delta_g",
            "cup_delta_peak_g",
        ):
            self.telemetry.pop(key, None)
        self._cup_baseline_g = None

    async def _finish_natural_water(self) -> None:
        try:
            if self.client is not None and self.client.is_connected:
                await self.client.quit_water_session()
            target = float(self.targets.get("volume_ml", 0))
            metered_value = self.telemetry.get("dispensed_water_peak_ml")
            metered = float(metered_value) if metered_value is not None else None
            tolerance = max(5.0, target * 0.05)
            result = "complete"
            if metered is None:
                result = "completion_unconfirmed"
                self.last_error = "brewer stopped but no metered water volume was observed"
            elif metered < target - tolerance:
                result = "completion_unconfirmed"
                self.last_error = (
                    f"brewer stopped early at {metered:.1f} ml; target was {target:.1f} ml"
                )
            elif metered > target + (tolerance * 2):
                result = "completion_unconfirmed"
                self.last_error = (
                    f"brewer reported {metered:.1f} ml; target was {target:.1f} ml"
                )
            self._finish_activity(
                result,
                target_volume_ml=target,
                metered_volume_ml=metered,
            )
        except Exception as exc:  # pragma: no cover - hardware cleanup path
            self.last_error = f"water completed but brewer exit failed: {exc}"
            self.phase = "cleanup_failed"

    def status(self) -> dict[str, Any]:
        firmware = self.machine_info.get("firmware")
        target = self.targets.get("target_dispensed_water_ml")
        if target is None:
            target = self.targets.get("volume_ml")
        dispensed = self.telemetry.get("dispensed_water_peak_ml")
        liquid_progress: dict[str, Any] = {}
        if target is not None:
            liquid_progress["target_dispensed_water_ml"] = target
        if dispensed is not None:
            liquid_progress["dispensed_water_ml"] = dispensed
        if target is not None and dispensed is not None:
            liquid_progress["remaining_ml"] = round(
                max(0.0, float(target) - float(dispensed)), 2
            )
            liquid_progress["dispensed_vs_target_ml"] = round(
                float(dispensed) - float(target), 2
            )
        if self.telemetry.get("cup_delta_peak_g") is not None:
            liquid_progress["cup_delta_g"] = self.telemetry["cup_delta_peak_g"]
        public_telemetry = dict(self.telemetry)
        if "dispensed_water_ml" in public_telemetry:
            public_telemetry["water_ml"] = public_telemetry["dispensed_water_ml"]
        if "dispensed_water_peak_ml" in public_telemetry:
            public_telemetry["water_peak_ml"] = public_telemetry[
                "dispensed_water_peak_ml"
            ]
        if "cup_weight_g" in public_telemetry:
            public_telemetry["coffee_g"] = public_telemetry["cup_weight_g"]
        recovery_records = [
            path.name
            for path in (self.coffee_state_file, self.tea_state_file)
            if path.exists()
        ]
        return {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "running": True,
            "operations": [
                "coffee",
                "tea",
                "scale",
                "grinder",
                "water",
                "presets",
                "settings",
                "advanced_tuning",
            ],
            "connected": self.connected,
            "machine": self.machine_name if self.connected else None,
            "address_configured": bool(self.address or self.default_address),
            "activity": self.activity,
            "phase": self.phase,
            "machine_state": self.machine_state,
            "firmware": firmware,
            "targets": dict(self.targets),
            "telemetry": public_telemetry,
            "liquid_progress": liquid_progress or None,
            "last_operation": dict(self.last_operation) if self.last_operation else None,
            "last_error": self.last_error,
            "recovery_records": recovery_records,
            "live_adjust": {
                "protocol_available": True,
                "hardware_verified": False,
                "command_encoding_apk_verified": True,
                "pattern_hardware_verified": firmware
                in LIVE_PATTERN_VERIFIED_FIRMWARE,
                "temperature_command_write_verified": True,
                "temperature_outlet_effect_measured": False,
                "temperature_hardware_verified": False,
                "enabled": self.environ.get(LIVE_ADJUST_ENV) == LIVE_ADJUST_SENTINEL,
                "scope": "freesolo_water_only",
            },
        }

    def events_since(self, since: int = 0) -> dict[str, Any]:
        since = max(0, int(since))
        events = [event for event in self._events if int(event["seq"]) > since]
        return {"events": events, "next_since": self._event_seq}

    async def _resolve_address(self, requested: str | None, timeout: float) -> tuple[str, str]:
        address = requested or self.default_address
        if address:
            return str(address), "xBloom Studio"
        devices = await self.scan_fn(timeout=float(timeout))
        if len(devices) != 1:
            raise BridgeError(f"expected exactly one nearby xBloom; found {len(devices)}")
        device = devices[0]
        return str(device.address), getattr(device, "name", None) or "xBloom Studio"

    async def _connect_unlocked(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("address")
        if self.connected:
            if requested and str(requested).casefold() != str(self.address).casefold():
                raise BridgeError("bridge already owns a different xBloom connection")
            return self.status()
        self.phase = "connecting"
        self.last_error = None
        self._machine_info_ready.clear()
        address, name = await self._resolve_address(
            str(requested) if requested else None,
            float(params.get("scan_timeout", 8.0)),
        )
        client = self.client_factory(address)
        client.add_event_listener(self._on_event)
        try:
            await client.connect()
            await client.open_session()
            self.client = client
            self.address = address
            self.machine_name = name
            self.phase = "idle"
            await client.request_status()
            try:
                await asyncio.wait_for(
                    self._machine_info_ready.wait(), timeout=self.machine_info_timeout
                )
            except asyncio.TimeoutError:
                self.last_error = "machine-info report not observed; writes remain gated"
        except Exception:
            client.remove_event_listener(self._on_event)
            try:
                await client.disconnect()
            except Exception:
                pass
            self.client = None
            self.phase = "disconnected"
            raise
        return self.status()

    async def _disconnect_unlocked(self) -> dict[str, Any]:
        if self.activity is not None:
            raise BridgeError("an activity is loaded or running; stop/cancel it first")
        client = self.client
        if client is not None:
            client.remove_event_listener(self._on_event)
            try:
                await client.close_session()
            finally:
                try:
                    await client.disconnect()
                finally:
                    self.client = None
                    self.address = None
                    self.machine_name = None
                    self.phase = "disconnected"
        return self.status()

    async def _ensure_connected(self, params: Mapping[str, Any]) -> None:
        if not self.connected:
            await self._connect_unlocked(params)

    def _require_idle_operation(self) -> None:
        if self.activity is not None:
            raise BridgeError(f"bridge is busy with {self.activity}:{self.phase}")
        if self.machine_state in ACTIVE_MACHINE_STATES:
            raise BridgeError(f"machine is not idle ({self.machine_state}); cancel first")

    def _require_idle_write_preflight(self) -> str:
        self._require_idle_operation()
        firmware = str(self.machine_info.get("firmware") or "")
        if firmware in SUPPORTED_FIRMWARE:
            return firmware
        if self.environ.get(UNTESTED_FIRMWARE_ENV) == UNTESTED_FIRMWARE_SENTINEL:
            return firmware or "unidentified"
        found = firmware or "unidentified"
        raise BridgeError(
            f"firmware {found} is not in the tested set {sorted(SUPPORTED_FIRMWARE)}; "
            f"restart the bridge with {UNTESTED_FIRMWARE_ENV}="
            f"{UNTESTED_FIRMWARE_SENTINEL} to accept this risk"
        )

    def _ensure_no_loaded_record(self) -> None:
        active = [
            path.name
            for path in (self.coffee_state_file, self.tea_state_file)
            if path.exists()
        ]
        if active:
            raise BridgeError(
                f"a loaded workflow record exists ({', '.join(active)}); recover/cancel first"
            )

    def _require_hot_water(self, confirmation: Any, expected: str) -> None:
        if self.environ.get(REMOTE_START_ENV) != REMOTE_START_SENTINEL:
            raise BridgeError(
                f"hot-water actions disabled; restart the bridge with "
                f"{REMOTE_START_ENV}={REMOTE_START_SENTINEL}"
            )
        if confirmation != expected:
            raise BridgeError(f"confirmation must equal {expected}")

    def _require_grinder(self, confirmation: Any) -> None:
        if self.environ.get(REMOTE_GRINDER_ENV) != REMOTE_GRINDER_SENTINEL:
            raise BridgeError(
                f"remote grinder disabled; restart the bridge with "
                f"{REMOTE_GRINDER_ENV}={REMOTE_GRINDER_SENTINEL}"
            )
        if confirmation != GRINDER_READY_SENTINEL:
            raise BridgeError(f"confirmation must equal {GRINDER_READY_SENTINEL}")

    def _require_live_adjust(self, confirmation: Any) -> None:
        if self.environ.get(LIVE_ADJUST_ENV) != LIVE_ADJUST_SENTINEL:
            raise BridgeError(
                "live FreeSolo adjustment is protocol-decoded but not hardware A/B verified; "
                f"restart the bridge with {LIVE_ADJUST_ENV}={LIVE_ADJUST_SENTINEL} for a "
                "supervised validation"
            )
        if confirmation != LIVE_ADJUST_SENTINEL:
            raise BridgeError(f"confirmation must equal {LIVE_ADJUST_SENTINEL}")

    def _require_settings_write(self, confirmation: Any, expected: str) -> None:
        if self.environ.get(SETTINGS_WRITE_ENV) != SETTINGS_WRITE_SENTINEL:
            raise BridgeError(
                f"persistent machine writes disabled; restart the bridge with "
                f"{SETTINGS_WRITE_ENV}={SETTINGS_WRITE_SENTINEL}"
            )
        if confirmation != expected:
            raise BridgeError(f"confirmation must equal {expected}")

    @staticmethod
    def _settings_view(info: Mapping[str, Any]) -> dict[str, Any]:
        keys = ("weight_unit", "temperature_unit", "water_source", "display")
        return {key: info.get(key) for key in keys}

    @staticmethod
    def _advanced_levels(
        values: Mapping[str, int], info: Mapping[str, Any]
    ) -> dict[str, Any]:
        radius = int(values["pour_radius"])
        vibration = int(values["vibration_amplitude"])
        radius_init = info.get("pouring_radius_init")
        vibration_init = info.get("vibration_init")
        radius_level: int | None = None
        if isinstance(radius_init, int) and (radius - radius_init) % 80 == 0:
            candidate = 3 + (radius - radius_init) // 80
            if 1 <= candidate <= 5:
                radius_level = candidate
        vibration_level: int | None = None
        if (vibration - 1000) % 100 == 0:
            candidate = 1 + (vibration - 1000) // 100
            if 1 <= candidate <= 6:
                vibration_level = candidate
        return {
            **dict(values),
            "pour_radius_init": radius_init,
            "pour_radius_level": radius_level,
            "vibration_init": vibration_init,
            "vibration_level": vibration_level,
        }

    async def _settings_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_no_loaded_record()
        await self._ensure_connected(params)
        self._require_idle_operation()
        info = _public_machine_info(await self.client.read_machine_info())
        self.machine_info.update(info)
        return {
            "settings": self._settings_view(info),
            "read_only": True,
            "firmware": info.get("firmware"),
        }

    async def _settings_write(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_settings_write(
            params.get("confirmation"), SETTINGS_CONFIRM_SENTINEL
        )
        requested = {
            key: params[key]
            for key in ("weight_unit", "temperature_unit", "water_source", "display")
            if params.get(key) is not None
        }
        if not requested:
            raise BridgeError("settings.write needs at least one setting")
        choices = {
            "weight_unit": {"ml", "g", "oz"},
            "temperature_unit": {"C", "F"},
            "water_source": {"tank", "tap"},
            "display": {"low", "medium", "high"},
        }
        invalid = {
            key: value
            for key, value in requested.items()
            if value not in choices[key]
        }
        if invalid:
            raise BridgeError(f"invalid machine settings: {invalid}")
        self._ensure_no_loaded_record()
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        before_info = _public_machine_info(await self.client.read_machine_info())
        before = self._settings_view(before_info)
        if any(before.get(key) is None for key in requested):
            raise BridgeError(
                "cannot safely write settings without a complete 40521 baseline"
            )
        try:
            readback_info = _public_machine_info(
                await self.client.set_machine_settings(**requested)
            )
            mismatches = {
                key: {"requested": value, "readback": readback_info.get(key)}
                for key, value in requested.items()
                if readback_info.get(key) != value
            }
            if mismatches:
                raise BridgeError(f"settings readback mismatch: {mismatches}")
        except Exception as exc:
            rollback = {key: before[key] for key in requested}
            try:
                restored = await self.client.set_machine_settings(**rollback)
                rollback_ok = all(
                    restored.get(key) == value for key, value in rollback.items()
                )
            except Exception:
                rollback_ok = False
            raise BridgeError(
                f"settings write failed; rollback_confirmed={rollback_ok}: {exc}"
            ) from exc
        self.machine_info.update(readback_info)
        return {
            "status": "written_and_read_back",
            "firmware": firmware,
            "before": {key: before[key] for key in requested},
            "requested": dict(requested),
            "readback": {key: readback_info[key] for key in requested},
            "protocol_source": "Android APK commands 8005/8010/4508/8103",
            "hardware_write_tested_by_project": False,
        }

    async def _advanced_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_no_loaded_record()
        await self._ensure_connected(params)
        self._require_idle_operation()
        info = _public_machine_info(await self.client.read_machine_info())
        values = await self.client.read_advanced_settings()
        self.machine_info.update(info)
        return {
            "settings": self._advanced_levels(values, info),
            "read_only": True,
            "firmware": info.get("firmware"),
        }

    async def _advanced_write(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_settings_write(
            params.get("confirmation"), ADVANCED_CONFIRM_SENTINEL
        )
        radius_level = params.get("pour_radius_level")
        vibration_level = params.get("vibration_level")
        if radius_level is None and vibration_level is None:
            raise BridgeError("advanced.write needs at least one level")
        if radius_level is not None and int(radius_level) not in range(1, 6):
            raise BridgeError("pour-radius level must be 1-5")
        if vibration_level is not None and int(vibration_level) not in range(1, 7):
            raise BridgeError("vibration level must be 1-6")
        self._ensure_no_loaded_record()
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        info = _public_machine_info(await self.client.read_machine_info())
        before = await self.client.read_advanced_settings()
        radius_target: int | None = None
        if radius_level is not None:
            baseline = info.get("pouring_radius_init")
            if not isinstance(baseline, int) or not 560 <= baseline <= 840:
                raise BridgeError(
                    "machine did not expose a safe pour-radius baseline (expected 560-840)"
                )
            radius_target = baseline + (int(radius_level) - 3) * 80
        vibration_target = (
            1000 + (int(vibration_level) - 1) * 100
            if vibration_level is not None
            else None
        )
        expected = {
            key: value
            for key, value in {
                "pour_radius": radius_target,
                "vibration_amplitude": vibration_target,
            }.items()
            if value is not None
        }
        try:
            readback = await self.client.write_advanced_settings(
                pour_radius=radius_target,
                vibration_amplitude=vibration_target,
            )
            mismatches = {
                key: {"requested": value, "readback": readback.get(key)}
                for key, value in expected.items()
                if readback.get(key) != value
            }
            if mismatches:
                raise BridgeError(f"advanced-settings readback mismatch: {mismatches}")
        except Exception as exc:
            try:
                restored = await self.client.write_advanced_settings(
                    pour_radius=(
                        before["pour_radius"] if radius_target is not None else None
                    ),
                    vibration_amplitude=(
                        before["vibration_amplitude"]
                        if vibration_target is not None
                        else None
                    ),
                )
                rollback_ok = all(
                    restored[key] == before[key] for key in expected
                )
            except Exception:
                rollback_ok = False
            raise BridgeError(
                f"advanced-settings write failed; rollback_confirmed={rollback_ok}: {exc}"
            ) from exc
        self.machine_info.update(info)
        return {
            "status": "written_and_read_back",
            "firmware": firmware,
            "before": self._advanced_levels(before, info),
            "readback": self._advanced_levels(readback, info),
            "protocol_source": "Android APK CodeModule2 commands 11506-11509",
            "hardware_write_tested_by_project": False,
        }

    async def _coffee_load(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_no_loaded_record()
        raw_path = params.get("recipe")
        if not raw_path:
            raise BridgeError("coffee.load requires a local recipe path")
        path = Path(str(raw_path)).expanduser().resolve(strict=True)
        from xbloom_safety import load_strict_recipe, recipe_summary

        recipe = load_strict_recipe(path)
        summary = recipe_summary(recipe, path)
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        event = await self.client.load_recipe(recipe)
        if event.state_name != "armed":
            raise BridgeError(f"machine did not arm; state={event.state_name}")
        state = {
            "address": self.address,
            "machine": self.machine_name,
            "recipe_path": str(path),
            "recipe_sha256": _sha256(path),
            "loaded_at": time.time(),
            "status": "armed",
            "firmware": firmware,
            "owner": "bridge",
            "serving_kind": summary["kind"],
            "machine_program": summary["machine_program"],
            "manual_preload_ice_g": summary["manual_preload_ice_g"],
        }
        _atomic_json(self.coffee_state_file, state, private=True)
        self.activity = "coffee"
        self.phase = "loaded"
        self.last_error = None
        self.targets = {
            "recipe": path.name,
            "target_dispensed_water_ml": recipe.total_machine_water_ml,
            "machine_program": summary["machine_program"],
            "machine_dispenses_ice": summary["machine_dispenses_ice"],
            "manual_preload_ice_g": summary["manual_preload_ice_g"],
        }
        self._saw_active = False
        return {
            "status": "armed",
            "recipe": path.name,
            "firmware": firmware,
            **summary,
        }

    async def _coffee_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_hot_water(params.get("confirmation"), READY_SENTINEL)
        if self.activity != "coffee" or self.phase != "loaded":
            raise BridgeError("bridge has no loaded coffee recipe")
        state = _read_json(self.coffee_state_file)
        age = time.time() - float(state.get("loaded_at", 0))
        if age < 0 or age > ARM_MAX_AGE_SECONDS:
            raise BridgeError("armed state is older than 5 minutes; load again")
        path = Path(str(state.get("recipe_path") or ""))
        if not path.is_file() or _sha256(path) != state.get("recipe_sha256"):
            raise BridgeError("recipe changed or disappeared since it was loaded")
        self._reset_liquid_telemetry()
        state.update(status="start_pending", start_requested_at=time.time())
        _atomic_json(self.coffee_state_file, state, private=True)
        self.phase = "starting"
        try:
            event = await self.client.start()
        except BaseException as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"coffee start outcome is unconfirmed: {exc}"
            state.update(
                status="start_unconfirmed",
                start_unconfirmed_at=time.time(),
                last_state=self.machine_state or state.get("last_state"),
            )
            _atomic_json(self.coffee_state_file, state, private=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BridgeError(
                f"{self.last_error}; inspect bridge events/status, then cancel or use "
                "the physical control; do not retry start"
            ) from exc
        self.phase = "running"
        self.last_error = None
        self._saw_active = event.state in ACTIVE_STATE_BYTES or self._saw_active
        state.update(status="running", started_at=time.time(), last_state=event.state_name)
        _atomic_json(self.coffee_state_file, state, private=True)
        return {
            "status": "running",
            "state": event.state_name,
            "machine_program": state.get("machine_program", "coffee-pour-over"),
            "machine_dispenses_ice": False,
            "manual_preload_ice_g": int(state.get("manual_preload_ice_g", 0) or 0),
        }

    async def _tea_load(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_no_loaded_record()
        raw_path = params.get("recipe")
        if not raw_path:
            raise BridgeError("tea.load requires a local recipe path")
        path = Path(str(raw_path)).expanduser().resolve(strict=True)
        from .tea import TeaRecipe

        recipe = TeaRecipe.from_yaml(path)
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        event = await self.client.load_tea_recipe(recipe)
        state = {
            "address": self.address,
            "machine": self.machine_name,
            "recipe_path": str(path),
            "recipe_sha256": _sha256(path),
            "loaded_at": time.time(),
            "status": "tea_loaded",
            "firmware": firmware,
            "owner": "bridge",
        }
        _atomic_json(self.tea_state_file, state, private=True)
        self.activity = "tea"
        self.phase = "loaded"
        self.last_error = None
        self.targets = {
            "recipe": path.name,
            "target_dispensed_water_ml": sum(pour.ml for pour in recipe.pours),
            "leaf_g": recipe.leaf_g,
            "steeps": len(recipe.pours),
        }
        self._saw_active = False
        return {
            "status": "tea_loaded",
            "recipe": path.name,
            "firmware": firmware,
            "ack": event.command_code,
            "summary": recipe.summary(),
        }

    async def _tea_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_hot_water(params.get("confirmation"), TEA_READY_SENTINEL)
        if self.activity != "tea" or self.phase != "loaded":
            raise BridgeError("bridge has no loaded tea recipe")
        state = _read_json(self.tea_state_file)
        age = time.time() - float(state.get("loaded_at", 0))
        if age < 0 or age > ARM_MAX_AGE_SECONDS:
            raise BridgeError("loaded tea state is older than 5 minutes; load again")
        path = Path(str(state.get("recipe_path") or ""))
        if not path.is_file() or _sha256(path) != state.get("recipe_sha256"):
            raise BridgeError("tea recipe changed or disappeared since it was loaded")
        self._reset_liquid_telemetry()
        self.phase = "starting"
        event = await self.client.start_tea()
        self.phase = "running"
        self.last_error = None
        # Dedicated tea activity reports do not consistently carry the generic
        # coffee active-state byte. A confirmed 4512 response is the activation
        # boundary; a later terminal state may safely finish the bridge activity.
        self._saw_active = True
        state.update(status="running", started_at=time.time(), last_state=event.state_name)
        _atomic_json(self.tea_state_file, state, private=True)
        return {
            "status": "running",
            "state": event.state_name,
            "ack": event.command_code,
        }

    async def _scale_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        duration = float(params.get("duration_s", 30.0))
        raw_tare = params.get("tare", False)
        if not isinstance(raw_tare, bool):
            raise BridgeError("scale tare must be a boolean")
        tare = raw_tare
        if not 0.1 <= duration <= 3600:
            raise BridgeError("scale duration must be 0.1-3600 seconds")
        self._ensure_no_loaded_record()
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        self.activity = "scale"
        self.phase = "starting"
        self.targets = {
            "duration_s": duration,
            "entry_auto_zero": True,
            "extra_tare": tare,
        }
        target_snapshot = dict(self.targets)
        self.last_error = None

        async def on_ready() -> None:
            if self.activity == "scale":
                self.phase = "running"

        async def ignore_reading(_event: StatusEvent) -> None:
            # The permanent event listener already records and publishes it.
            return None

        async def run() -> None:
            try:
                await self.client.stream_scale(
                    ignore_reading,
                    duration=duration,
                    tare=tare,
                    on_ready=on_ready,
                )
            except asyncio.CancelledError:
                if self.activity == "scale":
                    self._finish_activity("stopped")
                raise
            except Exception as exc:
                if self.activity == "scale":
                    self._finish_activity("failed")
                self.last_error = f"scale session failed: {exc}"
            else:
                if self.activity == "scale":
                    self._finish_activity("complete")
            finally:
                if asyncio.current_task() is self._scale_task:
                    self._scale_task = None

        self._scale_task = asyncio.create_task(run())
        await asyncio.sleep(0)
        return {
            "status": self.phase,
            "firmware": firmware,
            **target_snapshot,
        }

    async def _scale_tare(self) -> dict[str, Any]:
        if self.activity != "scale" or self.phase != "running":
            raise BridgeError("scale tare requires a running scale session")
        await self.client.tare_scale()
        return {
            "status": "running",
            "activity": "scale",
            "command_write_verified": True,
            "report_observed": False,
        }

    async def _stop_scale(self, reason: str) -> dict[str, Any]:
        task = self._scale_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.activity == "scale":
            self._finish_activity(reason)
        return {"status": "stopped", "activity": "scale"}

    async def _save_presets(self, params: Mapping[str, Any]) -> dict[str, Any]:
        raw_recipes = params.get("recipes")
        if not isinstance(raw_recipes, list) or len(raw_recipes) != 3:
            raise BridgeError("presets.save requires exactly three recipe paths (A/B/C)")
        self._ensure_no_loaded_record()
        from xbloom_safety import load_strict_recipe, validate_slot_compatible

        paths = [Path(str(item)).expanduser().resolve(strict=True) for item in raw_recipes]
        recipes = [load_strict_recipe(path) for path in paths]
        for recipe in recipes:
            validate_slot_compatible(recipe)
        scale = params.get("scale", True)
        if not isinstance(scale, (bool, list)):
            raise BridgeError("presets scale must be a boolean or three booleans")
        if isinstance(scale, list) and len(scale) != 3:
            raise BridgeError("presets scale list must have exactly three values")
        if isinstance(scale, list) and not all(isinstance(value, bool) for value in scale):
            raise BridgeError("presets scale list values must be booleans")
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        self.activity = "presets"
        self.phase = "writing"
        self.targets = {"slots": [recipe.name for recipe in recipes]}
        try:
            await self.client.save_slots(recipes, scale=scale)
        except Exception as exc:
            self._finish_activity("write_unconfirmed")
            self.last_error = f"A/B/C preset write outcome is unconfirmed: {exc}"
            raise BridgeError(self.last_error) from exc
        names = [recipe.name for recipe in recipes]
        self._finish_activity("saved")
        self.last_error = None
        return {
            "status": "saved",
            "firmware": firmware,
            "slots": names,
            "brew_started": False,
        }

    def _check_grinder_rest(self) -> None:
        if not self.grinder_state_file.exists():
            return
        try:
            state = _read_json(self.grinder_state_file)
        except BridgeError as exc:
            raise BridgeError(
                "grinder rest record is unreadable; inspect it before running the motor"
            ) from exc
        if state.get("in_progress"):
            raise BridgeError("a previous grinder bridge session lacks a verified stop")
        remaining = float(state.get("blocked_until", 0)) - time.time()
        if remaining > 0:
            raise BridgeError(
                f"grinder rest interval active; wait {int(remaining + 0.999)} more seconds"
            )

    def _write_grinder_running_record(self, seconds: float) -> None:
        _atomic_json(
            self.grinder_state_file,
            {
                "in_progress": True,
                "started_at": time.time(),
                "requested_runtime_s": float(seconds),
                "owner": "bridge",
            },
            private=True,
        )

    def _write_grinder_stopped_record(self) -> None:
        now = time.time()
        _atomic_json(
            self.grinder_state_file,
            {
                "in_progress": False,
                "stopped_at": now,
                "blocked_until": now + GRINDER_REST_SECONDS,
                "owner": "bridge",
            },
            private=True,
        )

    def _cancel_grinder_timer(self) -> None:
        task = self._grinder_timer
        self._grinder_timer = None
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()

    def _start_grinder_timer(self) -> None:
        self._cancel_grinder_timer()
        self._grinder_started_at = time.monotonic()

        async def timer() -> None:
            try:
                await asyncio.sleep(self._grinder_remaining)
                async with self._op_lock:
                    if self.activity == "grinder" and self.phase == "running":
                        try:
                            await self._stop_grinder("runtime_elapsed")
                        except Exception as exc:  # safety state is retained for recovery
                            self.last_error = str(exc)
            except asyncio.CancelledError:
                return

        self._grinder_timer = asyncio.create_task(timer())

    async def _grinder_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_grinder(params.get("confirmation"))
        size = int(params.get("size", 0))
        rpm = int(params.get("rpm", 100))
        seconds = float(params.get("seconds", 0))
        if not 1 <= size <= 80:
            raise BridgeError("grind size must be 1-80")
        if not 60 <= rpm <= 120:
            raise BridgeError("grinder RPM must be 60-120")
        if not 0.1 <= seconds <= 30:
            raise BridgeError("grinder runtime must be 0.1-30 seconds")
        self._ensure_no_loaded_record()
        self._check_grinder_rest()
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        self._write_grinder_running_record(seconds)
        self.activity = "grinder"
        self.phase = "starting"
        self.targets = {"size": size, "rpm": rpm, "runtime_s": seconds}
        try:
            await self.client.start_grinder_session(size, rpm)
        except Exception as exc:
            await self._abort_grinder_after_control_error("start", exc)
        self.phase = "running"
        self.last_error = None
        self._grinder_remaining = seconds
        self._start_grinder_timer()
        return {"status": "running", "firmware": firmware, **self.targets}

    async def _abort_grinder_after_control_error(
        self, operation: str, cause: Exception
    ) -> None:
        """Fail closed when a motor command may have taken effect without an ACK."""
        self._cancel_grinder_timer()
        try:
            await self.client.stop_grinder_session()
        except Exception as stop_exc:
            self.phase = "stop_unconfirmed"
            self.last_error = (
                f"grinder {operation} failed and STOP/QUIT is unconfirmed: {stop_exc}"
            )
            raise BridgeError(self.last_error) from stop_exc
        self._write_grinder_stopped_record()
        self._finish_activity(f"{operation}_failed_stopped")
        self.last_error = f"grinder {operation} failed; STOP/QUIT was confirmed"
        raise BridgeError(self.last_error) from cause

    def _cancel_water_timer(self) -> None:
        task = self._water_timer
        self._water_timer = None
        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()

    def _start_water_timer(self, timeout: float) -> None:
        self._cancel_water_timer()

        async def timer() -> None:
            try:
                await asyncio.sleep(timeout)
                async with self._op_lock:
                    if self.activity == "water":
                        self.last_error = (
                            "water completion was not observed before the safety timeout"
                        )
                        try:
                            await self._stop_water("safety_timeout_stopped")
                        except Exception as exc:  # state is retained for manual recovery
                            self.last_error = str(exc)
            except asyncio.CancelledError:
                return

        self._water_timer = asyncio.create_task(timer())

    async def _water_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_hot_water(params.get("confirmation"), WATER_READY_SENTINEL)
        volume = float(params.get("volume_ml", 0))
        temp = int(params.get("temp_c", -1))
        flow = float(params.get("flow_ml_s", 3.5))
        pattern = str(params.get("pattern", "center"))
        if not 20 <= volume <= 360:
            raise BridgeError("water volume must be 20-360 ml")
        if temp != ROOM_TEMPERATURE_C and not 40 <= temp <= 98:
            raise BridgeError("water temperature must be RT or 40-98 C")
        flow10 = round(flow * 10)
        if flow10 not in range(30, 36) or abs(flow10 / 10 - flow) > 1e-6:
            raise BridgeError("water flow must be 3.0-3.5 ml/s in 0.1 steps")
        if pattern not in {"center", "spiral", "circular", "ring"}:
            raise BridgeError("water pattern must be center, spiral, or circular")
        if pattern == "ring":
            pattern = "circular"
        source = str(params.get("water_source", "auto"))
        if source not in {"auto", "tank", "tap"}:
            raise BridgeError("water source must be auto, tank, or tap")
        self._ensure_no_loaded_record()
        await self._ensure_connected(params)
        firmware = self._require_idle_write_preflight()
        if source == "auto":
            source = str(self.machine_info.get("water_source") or "")
        if source not in {"tank", "tap"}:
            raise BridgeError("water source must be tank/tap or readable via auto")
        water_feed = {"tank": 0, "tap": 1}[source]
        self.activity = "water"
        self.phase = "starting"
        self._reset_liquid_telemetry()
        self.telemetry.pop("applied_pattern", None)
        self.telemetry.pop("applied_temperature_value", None)
        self.targets = {
            "volume_ml": volume,
            "temp_c": temp,
            "temp_setting": "RT" if temp == ROOM_TEMPERATURE_C else f"{temp} C",
            "flow_ml_s": flow,
            "pattern": pattern,
            "water_source": source,
            "safety_timeout_s": round(min(360.0, volume / flow + 180.0), 1),
        }
        try:
            await self.client.start_water_session(
                volume,
                temp,
                flow_ml_s=flow,
                pattern=pattern,
                water_feed=water_feed,
            )
        except Exception as exc:
            self.phase = "stopping"
            try:
                await self.client.stop_water_session()
            except Exception as stop_exc:
                self.phase = "stop_unconfirmed"
                self.last_error = (
                    f"water start failed and STOP/QUIT is unconfirmed: {stop_exc}"
                )
                raise BridgeError(self.last_error) from stop_exc
            self._finish_activity("start_failed_stopped")
            self.last_error = "water start failed; STOP/QUIT was confirmed"
            raise BridgeError(self.last_error) from exc
        self.phase = "running"
        self.last_error = None
        self._start_water_timer(float(self.targets["safety_timeout_s"]))
        return {"status": "running", "firmware": firmware, **self.targets}

    async def _pause(self) -> dict[str, Any]:
        if self.activity not in {"coffee", "grinder", "water"} or self.phase != "running":
            raise BridgeError("pause requires a running coffee, grinder, or water activity")
        activity = self.activity
        try:
            if activity == "coffee":
                event = await self.client.pause_coffee()
            elif activity == "grinder":
                if self._grinder_started_at is not None:
                    self._grinder_remaining = max(
                        0.0,
                        self._grinder_remaining
                        - (time.monotonic() - self._grinder_started_at),
                    )
                self._cancel_grinder_timer()
                event = await self.client.pause_grinder()
            else:
                event = await self.client.pause_water()
        except Exception as exc:
            if activity == "grinder":
                await self._abort_grinder_after_control_error("pause", exc)
            self.phase = "control_unconfirmed"
            self.last_error = f"{activity} pause outcome is unconfirmed: {exc}"
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        if self.activity != activity:
            return {
                "status": self.phase,
                "activity": self.activity,
                "ack": event.command_code,
                "terminal_during_control": True,
            }
        self.phase = "paused"
        return {"status": "paused", "activity": activity, "ack": event.command_code}

    async def _resume(self) -> dict[str, Any]:
        if self.activity not in {"coffee", "grinder", "water"} or self.phase != "paused":
            raise BridgeError("resume requires a paused coffee, grinder, or water activity")
        activity = self.activity
        if activity == "grinder" and self._grinder_remaining <= 0:
            raise BridgeError("grinder runtime is already exhausted; stop it")
        try:
            if activity == "coffee":
                event = await self.client.resume_coffee()
            elif activity == "grinder":
                event = await self.client.resume_grinder()
                self._start_grinder_timer()
            else:
                event = await self.client.resume_water()
        except Exception as exc:
            if activity == "grinder":
                await self._abort_grinder_after_control_error("resume", exc)
            self.phase = "control_unconfirmed"
            self.last_error = f"{activity} resume outcome is unconfirmed: {exc}"
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        if self.activity != activity:
            return {
                "status": self.phase,
                "activity": self.activity,
                "ack": event.command_code,
                "terminal_during_control": True,
            }
        self.phase = "running"
        return {"status": "running", "activity": activity, "ack": event.command_code}

    async def _stop_grinder(self, reason: str) -> dict[str, Any]:
        self.phase = "stopping"
        self._cancel_grinder_timer()
        try:
            event = await self.client.stop_grinder_session()
        except Exception as exc:
            self.phase = "stop_unconfirmed"
            self.last_error = f"grinder STOP/QUIT is unconfirmed: {exc}"
            raise BridgeError(self.last_error) from exc
        self._write_grinder_stopped_record()
        self._finish_activity(reason)
        return {"status": "stopped", "activity": "grinder", "ack": event.command_code}

    async def _stop_water(self, reason: str) -> dict[str, Any]:
        self.phase = "stopping"
        self._cancel_water_timer()
        target = float(self.targets.get("volume_ml", 0.0))
        metered_value = self.telemetry.get("dispensed_water_peak_ml")
        metered = float(metered_value) if metered_value is not None else None
        try:
            event = await self.client.stop_water_session()
        except Exception as exc:
            self.phase = "stop_unconfirmed"
            self.last_error = f"water STOP/QUIT is unconfirmed: {exc}"
            raise BridgeError(self.last_error) from exc
        self._finish_activity(
            reason,
            target_volume_ml=target,
            metered_volume_ml=metered,
        )
        return {"status": "stopped", "activity": "water", "ack": event.command_code}

    async def _recover_loaded_record(self) -> dict[str, Any] | None:
        records = [
            ("coffee", self.coffee_state_file),
            ("tea", self.tea_state_file),
        ]
        existing = [(kind, path) for kind, path in records if path.exists()]
        if not existing:
            return None
        if len(existing) != 1:
            raise BridgeError("multiple loaded workflow records exist; inspect them manually")
        kind, path = existing[0]
        state = _read_json(path)
        address = state.get("address") or self.default_address
        if not address:
            raise BridgeError("loaded workflow record has no machine address")
        await self._connect_unlocked({"address": str(address)})
        self.activity = kind
        self.phase = "recovering"
        self.targets = {"recovered_record": path.name}
        try:
            if kind == "coffee":
                await self.client.cancel_brew()
            else:
                await self.client.unload_tea_recipe()
        except Exception as exc:
            self.phase = "stop_unconfirmed"
            self.last_error = f"{kind} recovery cancel is unconfirmed: {exc}"
            raise BridgeError(self.last_error) from exc
        _unlink(path)
        self._finish_activity("recovery_cancel_sent")
        self.last_error = None
        return {
            "status": "recovery_cancel_sent",
            "activity": kind,
            "record_cleared": True,
        }

    async def _stop(self) -> dict[str, Any]:
        if self.activity is None:
            recovered = await self._recover_loaded_record()
            if recovered is not None:
                return recovered
            raise BridgeError("there is no bridge-owned activity to stop")
        if self.activity == "coffee":
            self.phase = "stopping"
            try:
                await self.client.cancel_brew()
            except Exception as exc:
                self.phase = "stop_unconfirmed"
                self.last_error = f"coffee cancel is unconfirmed: {exc}"
                raise BridgeError(self.last_error) from exc
            _unlink(self.coffee_state_file)
            self._finish_activity("cancel_sent")
            return {"status": "cancel_sent", "activity": "coffee"}
        if self.activity == "tea":
            self.phase = "stopping"
            try:
                await self.client.unload_tea_recipe()
            except Exception as exc:
                self.phase = "stop_unconfirmed"
                self.last_error = f"tea cancel/exit is unconfirmed: {exc}"
                raise BridgeError(self.last_error) from exc
            _unlink(self.tea_state_file)
            self._finish_activity("cancel_sent")
            return {"status": "cancel_sent", "activity": "tea"}
        if self.activity == "scale":
            return await self._stop_scale("stopped")
        if self.activity == "grinder":
            return await self._stop_grinder("stopped")
        if self.activity == "water":
            return await self._stop_water("stopped")
        raise BridgeError(f"stop is not implemented for activity {self.activity}")

    async def _set_water_temperature(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_adjust(params.get("confirmation"))
        if self.activity != "water" or self.phase not in {"running", "paused"}:
            raise BridgeError("temperature adjustment requires running/paused FreeSolo water")
        temp = int(params.get("temp_c", -1))
        if temp != ROOM_TEMPERATURE_C and not 40 <= temp <= 98:
            raise BridgeError("water temperature must be RT or 40-98 C")
        try:
            event = await self.client.set_water_temperature(temp)
        except Exception as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"water temperature adjustment outcome is unconfirmed: {exc}"
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        self.targets["temp_c"] = temp
        self.targets["temp_setting"] = "RT" if temp == ROOM_TEMPERATURE_C else f"{temp} C"
        return {
            "status": self.phase,
            "activity": "water",
            "target_temp_c": temp,
            "report": event.command_code if event is not None else None,
            "report_observed": event is not None,
            "command_write_verified": True,
            "outlet_temperature_effect_measured": False,
            # Compatibility field retained for existing bridge consumers. A
            # correct BLE write is not a physical outlet-temperature measure.
            "hardware_effect_verified": False,
        }

    async def _set_water_pattern(self, params: Mapping[str, Any]) -> dict[str, Any]:
        self._require_live_adjust(params.get("confirmation"))
        if self.activity != "water" or self.phase not in {"running", "paused"}:
            raise BridgeError("pattern adjustment requires running/paused FreeSolo water")
        pattern = str(params.get("pattern", ""))
        if pattern not in {"center", "spiral", "circular", "ring"}:
            raise BridgeError("pattern must be center, spiral, or circular")
        if pattern == "ring":
            pattern = "circular"
        try:
            event = await self.client.set_water_pattern(pattern)
        except Exception as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"water pattern adjustment outcome is unconfirmed: {exc}"
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        self.targets["pattern"] = pattern
        firmware = str(self.machine_info.get("firmware") or "")
        verified = firmware in LIVE_PATTERN_VERIFIED_FIRMWARE
        return {
            "status": self.phase,
            "activity": "water",
            "target_pattern": pattern,
            "report": event.command_code if event is not None else None,
            "report_observed": event is not None,
            "hardware_effect_verified": verified,
            "verified_firmware": firmware if verified else None,
        }

    async def rpc(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = {} if params is None else dict(params)
        if method in {"ping", "status"}:
            return self.status()
        if method == "events":
            return self.events_since(int(params.get("since", 0)))
        async with self._op_lock:
            if method == "connect":
                return await self._connect_unlocked(params)
            if method == "disconnect":
                return await self._disconnect_unlocked()
            if method == "settings.read":
                return await self._settings_read(params)
            if method == "settings.write":
                return await self._settings_write(params)
            if method == "advanced.read":
                return await self._advanced_read(params)
            if method == "advanced.write":
                return await self._advanced_write(params)
            if method == "coffee.load":
                return await self._coffee_load(params)
            if method == "coffee.start":
                return await self._coffee_start(params)
            if method == "tea.load":
                return await self._tea_load(params)
            if method == "tea.start":
                return await self._tea_start(params)
            if method == "scale.start":
                return await self._scale_start(params)
            if method == "scale.tare":
                return await self._scale_tare()
            if method == "presets.save":
                return await self._save_presets(params)
            if method == "grinder.start":
                return await self._grinder_start(params)
            if method == "water.start":
                return await self._water_start(params)
            if method == "pause":
                return await self._pause()
            if method == "resume":
                return await self._resume()
            if method in {"stop", "cancel"}:
                return await self._stop()
            if method == "water.set_temperature":
                return await self._set_water_temperature(params)
            if method == "water.set_pattern":
                return await self._set_water_pattern(params)
        raise BridgeError(f"unknown bridge method {method}")

    async def shutdown(self, *, force: bool = False) -> None:
        async with self._op_lock:
            if self.activity is not None:
                if not force:
                    raise BridgeError(
                        f"bridge owns {self.activity}:{self.phase}; stop/cancel before shutdown"
                    )
                await self._stop()
            elif force and (self.coffee_state_file.exists() or self.tea_state_file.exists()):
                await self._stop()
            await self._disconnect_unlocked()


class BridgeServer:
    """Authenticated loopback JSON-line server."""

    def __init__(
        self,
        core: BridgeCore,
        *,
        record_path: Path | None = None,
        token: str | None = None,
    ) -> None:
        self.core = core
        self.record_path = Path(record_path) if record_path is not None else bridge_record_path()
        self.token = token or secrets.token_urlsafe(32)
        self.shutdown_event = asyncio.Event()
        self.server: asyncio.Server | None = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id: Any = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            if not line or len(line) > 65536:
                raise BridgeError("invalid or oversized bridge request")
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise BridgeError("bridge request must be an object")
            request_id = request.get("id")
            supplied = str(request.get("token") or "")
            if not secrets.compare_digest(supplied, self.token):
                raise BridgeError("bridge authentication failed")
            method = str(request.get("method") or "")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise BridgeError("bridge params must be an object")
            if method == "shutdown":
                await self.core.shutdown(force=bool(params.get("force", False)))
                result = {"status": "shutting_down"}
                self.shutdown_event.set()
            else:
                result = await self.core.rpc(method, params)
            response = {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": type(exc).__name__,
            }
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, BRIDGE_HOST, 0, limit=65536
        )
        socket_info = self.server.sockets[0].getsockname()
        record = {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "pid": os.getpid(),
            "host": BRIDGE_HOST,
            "port": int(socket_info[1]),
            "token": self.token,
            "started_at": time.time(),
        }
        _atomic_json(self.record_path, record, private=True)
        try:
            await self.shutdown_event.wait()
        finally:
            self.server.close()
            await self.server.wait_closed()
            try:
                current = _read_json(self.record_path)
            except BridgeError:
                current = {}
            if current.get("token") == self.token:
                _unlink(self.record_path)


def bridge_call(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    record_path: Path | None = None,
) -> dict[str, Any]:
    path = Path(record_path) if record_path is not None else bridge_record_path()
    record = _read_json(path)
    host = str(record.get("host") or "")
    if host != BRIDGE_HOST:
        raise BridgeError("bridge record does not point to the required loopback host")
    request = {
        "id": secrets.token_hex(8),
        "token": record.get("token"),
        "method": method,
        "params": dict(params or {}),
    }
    try:
        with socket.create_connection(
            (host, int(record["port"])),
            timeout=float(timeout),
        ) as connection:
            connection.settimeout(float(timeout))
            connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    raise BridgeError("bridge response exceeded 1 MB")
    except (OSError, KeyError, ValueError) as exc:
        raise BridgeError("bridge is not responding; inspect bridge.log or restart it") from exc
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise BridgeError("bridge returned an invalid response") from exc
    if not response.get("ok"):
        raise BridgeError(str(response.get("error") or "bridge request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise BridgeError("bridge returned a non-object result")
    return result


def bridge_is_running(*, record_path: Path | None = None) -> bool:
    try:
        bridge_call("ping", timeout=0.5, record_path=record_path)
        return True
    except BridgeError:
        return False


def start_bridge_daemon(
    script_path: Path,
    *,
    address: str | None = None,
    record_path: Path | None = None,
) -> dict[str, Any]:
    path = Path(record_path) if record_path is not None else bridge_record_path()
    if bridge_is_running(record_path=path):
        return bridge_call("status", record_path=path)
    _unlink(path)
    log_path = path.parent / BRIDGE_LOG_NAME
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(script_path).resolve())]
    if address:
        command.extend(["--address", str(address)])
    command.extend(["bridge", "serve"])
    creationflags = 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            command,
            cwd=Path(script_path).resolve().parents[1],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **popen_kwargs,
        )
    deadline = time.monotonic() + 8.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            return bridge_call("status", timeout=0.5, record_path=path)
        except BridgeError as exc:
            last_error = exc
    raise BridgeError(f"bridge did not start; inspect {log_path}: {last_error}")


async def serve_bridge(*, address: str | None = None) -> None:
    core = BridgeCore(default_address=address)
    server = BridgeServer(core)
    await server.run()

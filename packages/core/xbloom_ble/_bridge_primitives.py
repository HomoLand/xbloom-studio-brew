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
from uuid import uuid4

from xbloom_paths import (
    environment_copy,
    environment_value,
    normalize_state_root,
    skill_state_dir as _shared_skill_state_dir,
    state_dir as _shared_state_dir,
)
from xbloom_storage import (
    CLEAR_RECOVERY,
    GRINDER_WORKFLOW_KINDS,
    IDEM_COMPLETED,
    IDEM_FAILED,
    IDEM_PENDING,
    StateStore,
    StorageError,
    content_sha256,
)

from . import __version__ as CORE_VERSION
from .client import XBloomClient, scan
from .protocol import ROOM_TEMPERATURE_C
from .telemetry import StatusEvent


# Wire/RPC protocol range (Phase 0.3+). v1 lacked required hello + RPC envelope.
# v2: hello + envelope. v3: mutating RPCs require request_id; workflow-bound
# control requires workflow_id (emergency stop is the explicit exception).
BRIDGE_PROTOCOL_VERSION = 3
RPC_PROTOCOL_MIN = 3
RPC_PROTOCOL_MAX = 3
RPC_PROTOCOL_CURRENT = 3
# Highest wire version that did not require hello/envelope (for upgrade detection).
LEGACY_RPC_PROTOCOL_MAX = 1
# Discovery record schema version (independent of the RPC wire version).
BRIDGE_RECORD_FORMAT_VERSION = 2

# Methods that enforce protocol-v3 request_id + SQLite idempotency.
# connect/disconnect are intentionally excluded: they are not machine-action
# idempotent and do not participate in this contract.
MUTATING_METHODS = frozenset(
    {
        "coffee.load",
        "coffee.start",
        "tea.load",
        "tea.start",
        "pause",
        "resume",
        "stop",
        "cancel",
        "grinder.start",
        "water.start",
        "scale.start",
        "scale.tare",
        "water.set_temperature",
        "water.set_pattern",
        "settings.write",
        "advanced.write",
        "presets.save",
    }
)
# Control methods that require a matching active workflow_id (unless emergency)
# and participate in the same idempotency contract as MUTATING_METHODS.
WORKFLOW_BOUND_METHODS = frozenset(
    {
        "coffee.start",
        "tea.start",
        "pause",
        "resume",
        "stop",
        "cancel",
        "scale.tare",
        "water.set_temperature",
        "water.set_pattern",
    }
)

BRIDGE_HOST = "127.0.0.1"
BRIDGE_RECORD_NAME = "bridge.json"
BRIDGE_LOCK_NAME = "bridge.lock"
BRIDGE_LOG_NAME = "bridge.log"
# Legacy filename constants for import-only path properties (coffee/tea tests
# and migration docs). Runtime never reads/writes these files. Grinder rest
# JSON is also import-only; its path lives only in xbloom_storage migration
# constants (GRINDER_REST_STATE_REL).
COFFEE_STATE_NAME = "armed-state.json"
TEA_STATE_NAME = "tea-loaded-state.json"

# Methods that may run without a prior compatible hello (diagnostics only).
DIAGNOSTIC_METHODS = frozenset({"hello", "ping", "status"})
# Phases that are safe for idle restart/upgrade when activity is None.
SAFE_IDLE_PHASES = frozenset({"disconnected", "idle"})
# Config keys that form the daemon config fingerprint (startup snapshot).
_CONFIG_FINGERPRINT_ENVS = (
    "XBLOOM_ADDRESS",
    "XBLOOM_ENABLE_REMOTE_START",
    "XBLOOM_ENABLE_REMOTE_GRINDER",
    "XBLOOM_ALLOW_UNTESTED_FIRMWARE",
    "XBLOOM_ENABLE_LIVE_ADJUST",
    "XBLOOM_ENABLE_SETTINGS_WRITE",
    "XBLOOM_BRIDGE_IDLE_DISCONNECT_S",
)

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


class BridgeError(RuntimeError):
    """Safe, user-facing bridge failure.

    Optional ``category`` is a stable client-facing class (for example
    ``device_busy_external``) without claiming more certainty than the
    underlying transport error supports.
    """

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        self.category = category


class BridgeCompatibilityError(BridgeError):
    """Client/server protocol or identity mismatch (no BLE side effects)."""


class BridgeLockError(BridgeError):
    """Another bridge instance holds the lifecycle lock."""


# Connect failures that often mean the phone/app or another client owns the
# radio. Unavailable vs already-connected vs GATT busy cannot always be
# distinguished from Bleak text alone -- surface one stable category.
_DEVICE_BUSY_EXTERNAL_MARKERS = (
    "already connected",
    "already in use",
    "device in use",
    "in use",
    "busy",
    "gatt",
    "not available",
    "unavailable",
    "connection failed",
    "failed to connect",
    "peer removed pairing",
    "disconnected",
    "access denied",
    "permission",
    "resource temporarily unavailable",
    "os error",
    "winerror",
    "bluetooth",
)

# Fresh machine states that positively prove a non-terminal coffee workflow.
_RECONCILE_ACTIVE_STATES = frozenset(
    {"starting", "brewing", "awaiting_confirm"}
)
_RECONCILE_TERMINAL_STATES = frozenset({"ready", "complete", "idle"})
_RECONCILE_LOADED_COFFEE_STATES = frozenset({"armed"})


class BridgeLock:
    """Cross-platform non-blocking exclusive lock for one state root.

    Uses ``fcntl.flock`` on Unix and ``msvcrt.locking`` on Windows. The lock is
    held for the lifetime of the owning process (or until :meth:`release`).
    """

    def __init__(self, state_root: Path) -> None:
        self.state_root = normalize_state_root(state_root)
        self.path = self.state_root / BRIDGE_LOCK_NAME
        self._fd: int | None = None
        self.owned = False

    def acquire(self, *, blocking: bool = False) -> bool:
        if self.owned:
            return True
        self.state_root.mkdir(parents=True, exist_ok=True)
        # O_RDWR|O_CREAT keeps a stable fd for msvcrt/fcntl; avoid shared
        # read/write races that raise PermissionError on Windows.
        flags = os.O_RDWR | os.O_CREAT
        if os.name == "nt":
            flags |= getattr(os, "O_BINARY", 0)
        fd = os.open(str(self.path), flags, 0o644)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    # Lock a single existing byte; do not truncate while held.
                    msvcrt.locking(fd, mode, 1)
                except OSError:
                    os.close(fd)
                    return False
            else:
                import fcntl

                flock_flags = fcntl.LOCK_EX
                if not blocking:
                    flock_flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(fd, flock_flags)
                except (BlockingIOError, OSError):
                    os.close(fd)
                    return False
                # Best-effort owner pid on Unix (fcntl does not pin file size).
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                except OSError:
                    pass
            self._fd = fd
            self.owned = True
            return True
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def release(self) -> None:
        if not self.owned or self._fd is None:
            return
        fd = self._fd
        self._fd = None
        self.owned = False
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> BridgeLock:
        if not self.acquire():
            raise BridgeLockError(f"bridge lock held at {self.path}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def skill_state_dir() -> Path:
    """Return the normalised state root (canonical + legacy env)."""

    return _shared_state_dir()


def bridge_record_path(state_root: Path | None = None) -> Path:
    root = normalize_state_root(state_root) if state_root is not None else skill_state_dir()
    return root / BRIDGE_RECORD_NAME


def bridge_lock_path(state_root: Path | None = None) -> Path:
    root = normalize_state_root(state_root) if state_root is not None else skill_state_dir()
    return root / BRIDGE_LOCK_NAME


def _normalize_address_identity(address: str | None) -> str:
    """Canonical form for fingerprinting MAC/UUID-style addresses."""

    if address is None:
        return ""
    return str(address).strip().casefold()


def config_fingerprint(
    environ: Mapping[str, str] | None = None,
    *,
    address: str | None = None,
) -> str:
    """SHA-256 of the *effective* config snapshot (not secrets).

    Uses the effective BLE address only (explicit ``address`` wins over env)
    so a shadowed ``XBLOOM_ADDRESS`` does not change the hash when callers
    pass the same explicit address. Address identity is strip/casefold
    normalized so equivalent MAC/UUID casing does not cause restart warnings.
    Gate env values remain behavior-relevant inputs; raw secrets are never
    included.
    """

    env = environment_copy(environ)
    # Effective values only: XBLOOM_ADDRESS is folded into ``address`` rather
    # than hashed twice (or under a shadowed env value when explicit).
    payload: dict[str, str] = {
        key: env.get(key, "")
        for key in _CONFIG_FINGERPRINT_ENVS
        if key != "XBLOOM_ADDRESS"
    }
    if address is not None:
        effective_address = str(address)
    else:
        effective_address = str(env.get("XBLOOM_ADDRESS") or "")
    payload["address"] = _normalize_address_identity(effective_address)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _core_version() -> str:
    return str(CORE_VERSION)


def _atomic_json(path: Path, data: Mapping[str, Any], *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(dict(data), indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
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


def _protocol_overlap(
    client_min: int, client_max: int, server_min: int, server_max: int
) -> bool:
    return max(client_min, server_min) <= min(client_max, server_max)


def _is_json_integer(value: Any) -> bool:
    """True for real JSON integers only (Python int, not bool/float/str)."""

    return isinstance(value, int) and not isinstance(value, bool)


def require_protocol_range(
    raw_min: Any,
    raw_max: Any,
    *,
    field_prefix: str = "protocol",
    require_present: bool = True,
) -> tuple[int, int]:
    """Validate protocol min/max as JSON integers with a consistent range.

    Rejects float (e.g. 2.9), bool, numeric strings, missing values (when
    required), reversed ranges, and non-positive bounds. Does not coerce.
    """

    label_min = f"{field_prefix}_min"
    label_max = f"{field_prefix}_max"
    if raw_min is None or raw_max is None:
        if require_present:
            raise BridgeError(f"{label_min}/{label_max} are required and must be integers")
        raise BridgeError(f"{label_min}/{label_max} must be integers")
    if not _is_json_integer(raw_min) or not _is_json_integer(raw_max):
        raise BridgeError(
            f"{label_min}/{label_max} must be JSON integers "
            "(not float, bool, or string)"
        )
    client_min = raw_min
    client_max = raw_max
    if client_min < 1 or client_max < 1:
        raise BridgeError(f"{label_min}/{label_max} must be >= 1")
    if client_min > client_max:
        raise BridgeError(f"{label_min} must be <= {label_max}")
    return client_min, client_max


def evaluate_compatibility(
    *,
    client_protocol_min: Any,
    client_protocol_max: Any,
    server_protocol_min: int = RPC_PROTOCOL_MIN,
    server_protocol_max: int = RPC_PROTOCOL_MAX,
    client_config_fingerprint: str | None = None,
    server_config_fingerprint: str | None = None,
    strict_protocol_types: bool = False,
) -> dict[str, Any]:
    """Return a public compatibility assessment (never includes the token).

    When ``strict_protocol_types`` is True (wire/public inputs), min/max must
    already be JSON integers and range-valid; otherwise a BridgeError is raised.
    """

    if strict_protocol_types:
        client_min, client_max = require_protocol_range(
            client_protocol_min, client_protocol_max
        )
    else:
        # Internal callers pass trusted ints; still guard against bool via type check.
        if not _is_json_integer(client_protocol_min) or not _is_json_integer(
            client_protocol_max
        ):
            client_min, client_max = require_protocol_range(
                client_protocol_min, client_protocol_max
            )
        else:
            client_min = client_protocol_min
            client_max = client_protocol_max
            if client_min < 1 or client_max < 1 or client_min > client_max:
                client_min, client_max = require_protocol_range(client_min, client_max)
    if not _is_json_integer(server_protocol_min) or not _is_json_integer(
        server_protocol_max
    ):
        raise BridgeError("server protocol_min/max must be JSON integers")
    protocol_ok = _protocol_overlap(
        client_min,
        client_max,
        server_protocol_min,
        server_protocol_max,
    )
    config_match: bool | None
    if client_config_fingerprint is None or server_config_fingerprint is None:
        config_match = None
    else:
        config_match = client_config_fingerprint == server_config_fingerprint
    compatible = protocol_ok
    reasons: list[str] = []
    if not protocol_ok:
        reasons.append(
            f"protocol range mismatch: client "
            f"[{client_min},{client_max}] vs server "
            f"[{server_protocol_min},{server_protocol_max}]"
        )
    if config_match is False:
        reasons.append("config fingerprint mismatch (daemon uses startup snapshot)")
    return {
        "compatible": compatible,
        "protocol_ok": protocol_ok,
        "config_match": config_match,
        "reasons": reasons,
        "server_protocol_min": server_protocol_min,
        "server_protocol_max": server_protocol_max,
        "server_protocol_current": RPC_PROTOCOL_CURRENT,
    }


def _public_machine_info(info: Mapping[str, Any] | None) -> dict[str, Any]:
    if not info:
        return {}
    return {key: value for key, value in info.items() if key != "serial_number"}


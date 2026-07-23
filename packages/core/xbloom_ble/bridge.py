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
COFFEE_STATE_NAME = "armed-state.json"
TEA_STATE_NAME = "tea-loaded-state.json"
GRINDER_STATE_NAME = "grinder-rest-state.json"

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
# distinguished from Bleak text alone — surface one stable category.
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
        store: StateStore | None = None,
    ) -> None:
        self.default_address = default_address or environment_value(
            "XBLOOM_ADDRESS", environ=environ
        )
        self.state_dir = (
            normalize_state_root(state_dir)
            if state_dir is not None
            else skill_state_dir()
        )
        self.client_factory = client_factory
        self.scan_fn = scan_fn
        self.environ = environment_copy(environ)
        self.machine_info_timeout = float(machine_info_timeout)
        self.instance_id = f"brg_{uuid4().hex}"
        self.config_fingerprint = config_fingerprint(
            self.environ, address=self.default_address
        )
        self.core_version = _core_version()
        self.started_at = time.time()

        # Durable state: inject for tests; otherwise own a store at state_dir.
        self._store_owned = store is None
        self.store = store if store is not None else StateStore(self.state_dir)
        self.store.ensure_schema()

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
        self.active_workflow_id: str | None = None
        self._recovery_required: bool = False
        self._recovery_detail: dict[str, Any] | None = None
        self._event_persist_failed: bool = False
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
        self._scale_stop_in_progress: bool = False
        self._cup_baseline_g: float | None = None
        # After daemon restart with durable loaded state, start requires an
        # explicit machine-state reconcile (request_status -> fresh armed) first.
        self._loaded_needs_reconcile: bool = False
        # Generation + event for state-bearing notifications (event.state is not
        # None). Reconcile clears/captures generation before status query and
        # awaits a strictly newer generation so stale machine_state cannot pass.
        self._state_notify_generation: int = 0
        self._state_notify_event = asyncio.Event()
        # BLE connection lifecycle (user-visible; daemon process stays up).
        # scope: explicit (debug connect), workflow (coffee/tea), one-shot
        # (grinder/water/scale and other auto-connects without workflow ownership).
        self.connection_scope: str | None = None
        self.release_pending: bool = False
        self.last_disconnect_reason: str | None = None
        self.last_disconnect_time: float | None = None
        self.last_disconnect_error: str | None = None
        self._pending_release_reason: str | None = None
        self._release_task: asyncio.Task[Any] | None = None
        # Orphan idle disconnect (A7): safety-net only for leftover auto-owned
        # links with no activity and no active/recovery workflow. Never armed by
        # status/events; never applies to explicit debug connections.
        self._idle_orphan_since: float | None = None
        self._idle_orphan_deadline: float | None = None
        self._idle_orphan_task: asyncio.Task[Any] | None = None
        # Client identity for disconnect callbacks: ignore stale expected drops
        # after terminal release so they cannot rewrite terminal → recovery.
        self._client_generation: int = 0
        self._bound_client_generation: int = 0
        # Reconstruct durable workflow identity without auto-connect / start.
        self._reconstruct_from_store()

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

    def _reconstruct_from_store(self) -> None:
        """Hydrate in-memory activity from durable state; never BLE connect/start."""

        try:
            active = self.store.get_active_workflow()
        except StorageError:
            return
        if active is None:
            return
        self.active_workflow_id = str(active["workflow_id"])
        kind = str(active.get("kind") or "")
        state = str(active.get("state") or "recovery")
        meta = dict(active.get("metadata") or {})
        # Map durable kind -> activity name.
        if kind in {"coffee", "coffee_recovery"}:
            self.activity = "coffee"
        elif kind in {"tea", "tea_recovery"}:
            self.activity = "tea"
        elif kind in {"grinder", "grinder_recovery"}:
            self.activity = "grinder"
        elif kind == "water":
            self.activity = "water"
        elif kind == "scale":
            self.activity = "scale"
        elif kind in {"settings", "advanced", "presets"}:
            self.activity = kind
        else:
            self.activity = kind or None
        # Never auto-start; surface recovery when prior start may be outstanding.
        # created/loading are unconfirmed -- never map them to loaded.
        if state in {"created", "loading"}:
            self.phase = state
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "durable_workflow_unconfirmed",
                "workflow_id": self.active_workflow_id,
                "state": state,
                "message": (
                    "durable workflow is created/loading (unconfirmed); "
                    "recovery_required -- do not start"
                ),
            }
        elif state in {"starting", "control_unconfirmed", "stop_unconfirmed", "recovery_required"}:
            self.phase = state if state != "recovery_required" else "control_unconfirmed"
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "durable_workflow_reconstructed",
                "workflow_id": self.active_workflow_id,
                "state": state,
            }
        elif state == "loaded":
            # Confirmed loaded may exist durably, but after process restart the
            # machine state is unconfirmed. Start requires explicit reconcile.
            self.phase = "loaded"
            self._loaded_needs_reconcile = True
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "durable_workflow_reconstructed_loaded",
                "workflow_id": self.active_workflow_id,
                "state": state,
                "message": (
                    "daemon restarted with loaded workflow; reconcile machine "
                    "state (armed) before start; never re-load"
                ),
            }
        elif state in {"running", "paused", "soaking", "stopping", "recovering", "recovery"}:
            self.phase = state if state != "recovery" else "recovering"
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "durable_workflow_reconstructed_running",
                "workflow_id": self.active_workflow_id,
                "state": state,
                "message": "daemon restarted with non-terminal workflow; do not retry start",
            }
        else:
            self.phase = state or "recovery"
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "durable_workflow_reconstructed",
                "workflow_id": self.active_workflow_id,
                "state": state,
            }
        # Targets from metadata when available (best-effort; no BLE).
        if isinstance(meta.get("targets"), dict):
            self.targets = dict(meta["targets"])
        address = meta.get("address") or active.get("owner")
        if isinstance(address, str) and address and ":" in address:
            # Prefer explicit machine address from metadata.
            pass
        machine_addr = meta.get("machine_address")
        if isinstance(machine_addr, str) and machine_addr:
            self.address = machine_addr
            self.default_address = self.default_address or machine_addr
        # Remain disconnected; connection_scope stays None until explicit reconnect.
        if self.phase not in {"disconnected"}:
            # In-memory phase reflects workflow, not BLE link.
            pass

    @staticmethod
    def _require_request_id(params: Mapping[str, Any]) -> str:
        raw = params.get("request_id")
        if raw is None or not str(raw).strip():
            raise BridgeError("request_id is required for machine-mutating RPCs")
        return str(raw).strip()

    def _semantic_params(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        workflow_id: str | None,
    ) -> dict[str, Any]:
        """Stable semantic identity for idempotency hashing (excludes request_id)."""

        ignored = {
            "request_id",
            "token",
            "protocol_min",
            "protocol_max",
            "client_name",
            "client_version",
            "config_fingerprint",
        }
        semantic = {
            key: value
            for key, value in params.items()
            if key not in ignored
        }
        # Normalize path-like recipe fields for stable hashing.
        if "recipe" in semantic and semantic["recipe"] is not None:
            try:
                semantic["recipe"] = str(
                    Path(str(semantic["recipe"])).expanduser().resolve()
                )
            except OSError:
                semantic["recipe"] = str(semantic["recipe"])
        if "recipes" in semantic and isinstance(semantic["recipes"], list):
            normalized_recipes: list[str] = []
            for item in semantic["recipes"]:
                try:
                    normalized_recipes.append(
                        str(Path(str(item)).expanduser().resolve())
                    )
                except OSError:
                    normalized_recipes.append(str(item))
            semantic["recipes"] = normalized_recipes
        semantic["workflow_id"] = workflow_id
        semantic["method"] = method
        return semantic

    def _workflow_id_hint(self, params: Mapping[str, Any]) -> str | None:
        """Semantic workflow_id from params, falling back to active (no gate)."""

        raw = params.get("workflow_id")
        if raw is not None and str(raw).strip():
            return str(raw).strip()
        if self.active_workflow_id:
            return str(self.active_workflow_id)
        return None

    def _caller_omitted_workflow_id(self, params: Mapping[str, Any]) -> bool:
        raw = params.get("workflow_id")
        return raw is None or not str(raw).strip()

    def _idempotency_preflight(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        workflow_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return cached completed result before phase/cooldown gates, or None.

        Pending exact duplicates raise recovery_required (never reissue).
        Method / semantic-params / workflow mismatches raise conflict.
        Failed rows return None so a later reserve may re-attempt pre-BLE work.

        Exact semantic identity is required. A completed duplicate may match
        using the *stored* workflow_id only when the caller OMITTED workflow_id
        (e.g. emergency stop after the workflow is already terminal). An
        explicitly supplied different workflow_id is always a conflict.
        """

        request_id = self._require_request_id(params)
        try:
            existing = self.store.get_idempotency(request_id)
        except StorageError as exc:
            raise BridgeError(str(exc)) from exc
        if existing is None:
            return None
        if existing.get("method") != method:
            raise BridgeError(
                f"idempotency conflict for request_id {request_id!r}: method mismatch"
            )

        def _sha_for(wid: str | None) -> str:
            return content_sha256(
                self._semantic_params(method, params, workflow_id=wid)
            )

        stored_wf = existing.get("workflow_id")
        stored_wf_norm = stored_wf if stored_wf else None
        caller_omitted = self._caller_omitted_workflow_id(params)
        status = existing.get("status")

        # Primary: exact match with the semantic workflow_id used for this call.
        if existing.get("params_sha256") == _sha_for(workflow_id):
            matched_wid: str | None = workflow_id
        elif (
            status == IDEM_COMPLETED
            and caller_omitted
            and stored_wf_norm is not None
            and existing.get("params_sha256") == _sha_for(stored_wf_norm)
        ):
            # Terminal already released active ownership; caller omitted
            # workflow_id and every other semantic param still matches.
            matched_wid = stored_wf_norm
        else:
            # Explicit different workflow_id (or any other param drift) is a
            # conflict even when the rest of the body matches.
            if (
                not caller_omitted
                and stored_wf_norm is not None
                and (workflow_id or None) != stored_wf_norm
            ):
                raise BridgeError(
                    f"idempotency conflict for request_id {request_id!r}: "
                    "workflow_id mismatch"
                )
            raise BridgeError(
                f"idempotency conflict for request_id {request_id!r}: "
                "params hash mismatch"
            )

        if status == IDEM_COMPLETED:
            return dict(existing.get("result") or {})
        if status == IDEM_PENDING:
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "pending_request",
                "request_id": request_id,
                "method": method,
                "workflow_id": matched_wid or workflow_id,
            }
            raise BridgeError(
                f"recovery_required: request_id {request_id!r} is pending "
                f"for {method}; do not retry the machine action"
            )
        # Failed: allow a fresh reservation after pre-BLE re-validation.
        if status == IDEM_FAILED:
            return None
        return None

    def _reserve_request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        semantic = self._semantic_params(method, params, workflow_id=workflow_id)
        try:
            reserved = self.store.reserve_idempotency(
                request_id,
                method,
                semantic,
                workflow_id=workflow_id,
            )
        except StorageError as exc:
            raise BridgeError(str(exc)) from exc
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return reserved
        if reserved.get("recovery_required") or (
            reserved.get("cached") and reserved.get("status") == IDEM_PENDING
        ):
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "pending_request",
                "request_id": request_id,
                "method": method,
                "workflow_id": workflow_id,
            }
            raise BridgeError(
                f"recovery_required: request_id {request_id!r} is pending "
                f"for {method}; do not retry the machine action"
            )
        return reserved

    def _complete_request(
        self, request_id: str, result: Mapping[str, Any]
    ) -> None:
        try:
            self.store.complete_idempotency(request_id, result)
        except StorageError as exc:
            # Completing idempotency after a confirmed machine write must not
            # invent a second write; surface recovery instead.
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "idempotency_complete_failed",
                "request_id": request_id,
                "error": str(exc),
            }
            raise BridgeError(
                f"persistence failed after machine action for request_id "
                f"{request_id!r}: {exc}; recovery_required"
            ) from exc

    def _fail_request(
        self,
        request_id: str,
        error: str,
        *,
        keep_pending: bool = False,
    ) -> None:
        try:
            self.store.fail_idempotency(
                request_id, error, keep_pending=keep_pending
            )
        except StorageError:
            pass

    def _require_active_workflow(
        self,
        params: Mapping[str, Any],
        *,
        emergency: bool = False,
    ) -> tuple[str, bool]:
        """Return (workflow_id, used_emergency). Reject stale/wrong IDs pre-BLE."""

        raw = params.get("workflow_id")
        active = self.active_workflow_id
        if emergency:
            # Emergency may ignore missing/stale IDs and act on the active workflow.
            if active is None and self.activity is None:
                # Fall through to JSON recovery records if present.
                return "", True
            return str(active or ""), True
        if raw is None or not str(raw).strip():
            raise BridgeError(
                "workflow_id is required (use emergency=true only for emergency stop)"
            )
        wid = str(raw).strip()
        if active is None:
            raise BridgeError(
                f"no active workflow; cannot apply workflow_id {wid!r}"
            )
        if wid != active:
            raise BridgeError(
                f"workflow_id {wid!r} does not match active workflow {active!r}"
            )
        return wid, False

    async def _await_fresh_state_notification(
        self, *, generation_before: int, timeout: float
    ) -> None:
        """Wait until a state-bearing notification advances past generation_before.

        Callers must capture ``_state_notify_generation`` *before* issuing the
        status query. A notification is fresh only if generation is strictly
        greater than that snapshot. ``request_status`` only writes a query and
        returns immediately; ``asyncio.sleep(0)`` is not proof of a reply.
        """

        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._state_notify_generation <= generation_before:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "no fresh state-bearing notification after status query"
                )
            self._state_notify_event.clear()
            # Re-check after clear to avoid missing a set that raced the clear.
            if self._state_notify_generation > generation_before:
                return
            try:
                await asyncio.wait_for(
                    self._state_notify_event.wait(), timeout=remaining
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "no fresh state-bearing notification after status query"
                ) from exc

    async def _reconcile_loaded_machine_state(
        self,
        *,
        kind: str,
        request_id: str | None = None,
    ) -> None:
        """Smallest safe gate after reconstructed loaded: query, never re-load/start.

        Awaits a *fresh* state-bearing notification caused after the status
        query begins (not stale ``machine_state``). Coffee may proceed only when
        that fresh state is exactly ``armed``. Tea has no positive loaded
        protocol marker in this codebase, so reconstructed tea always remains
        ``recovery_required`` and never issues ``start_tea``. On failure, no
        start write is issued; durable workflow ownership and BLE connection
        are retained for cancel/recovery.
        """

        if self.client is None or not self.connected:
            if request_id:
                self._fail_request(
                    request_id,
                    "loaded reconcile requires connection",
                    keep_pending=False,
                )
            raise BridgeError(
                "recovery_required: reconstructed loaded workflow cannot "
                "reconcile machine state without a connection; do not start"
            )

        timeout = self.machine_info_timeout
        generation_before = self._state_notify_generation
        self._state_notify_event.clear()
        try:
            await self.client.request_status()
            await self._await_fresh_state_notification(
                generation_before=generation_before, timeout=timeout
            )
        except Exception as exc:
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "loaded_reconcile_failed",
                "workflow_id": self.active_workflow_id,
                "kind": kind,
                "error": str(exc),
                "machine_state": self.machine_state,
            }
            if request_id:
                self._fail_request(request_id, str(exc), keep_pending=False)
            raise BridgeError(
                f"recovery_required: reconstructed loaded cannot query machine "
                f"state: {exc}; do not start"
            ) from exc

        if kind == "tea":
            # Protocol has no positive tea-loaded state marker. Idle/None/any
            # non-active state is not proof the recipe is still loaded.
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "loaded_reconcile_tea_no_positive_marker",
                "workflow_id": self.active_workflow_id,
                "kind": kind,
                "machine_state": self.machine_state,
                "message": (
                    "reconstructed tea loaded cannot confirm recipe still loaded "
                    "(no positive protocol marker); cancel or recover; do not start"
                ),
            }
            if request_id:
                self._fail_request(
                    request_id,
                    "tea has no positive loaded protocol marker after status query",
                    keep_pending=False,
                )
            raise BridgeError(
                "recovery_required: reconstructed tea loaded cannot confirm "
                f"recipe still loaded (machine_state={self.machine_state!r}; "
                "no positive protocol marker); do not start; never re-load; "
                "cancel or recover"
            )

        confirmed = kind == "coffee" and self.machine_state == "armed"

        if not confirmed:
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "loaded_reconcile_not_armed",
                "workflow_id": self.active_workflow_id,
                "kind": kind,
                "machine_state": self.machine_state,
            }
            if request_id:
                self._fail_request(
                    request_id,
                    f"machine state {self.machine_state!r} is not armed",
                    keep_pending=False,
                )
            raise BridgeError(
                "recovery_required: reconstructed loaded cannot confirm armed "
                f"state (machine_state={self.machine_state!r}); do not start; "
                "never re-load"
            )
        self._loaded_needs_reconcile = False
        self._recovery_required = False
        self._recovery_detail = None

    async def _recovery_reconcile(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Explicit recovery RPC: connect + query only; never load/start/control.

        Requires matching active ``workflow_id``. Uses the fresh state-generation
        gate (not stale ``machine_state``). Clears recovery only after durable
        reconciliation succeeds. No automatic periodic reconnect.
        """

        workflow_id, _ = self._require_active_workflow(params)
        if self.activity is None and self.active_workflow_id is None:
            raise BridgeError("recovery.reconcile requires an active durable workflow")

        # One connect attempt if needed; never retry within this operation.
        connect_established = False
        if not self.connected:
            try:
                await self._connect_unlocked(params, scope="workflow")
                connect_established = True
            except BridgeError as exc:
                # External busy / connect failure: retain durable ownership.
                self._recovery_required = True
                category = getattr(exc, "category", None) or self._classify_connect_failure(
                    exc
                )
                self._recovery_detail = {
                    "reason": "recovery_reconcile_connect_failed",
                    "workflow_id": workflow_id,
                    "category": category,
                    "error": str(exc),
                    "message": (
                        "recovery.reconcile connect failed; durable ownership retained; "
                        "one attempt only; do not auto-retry"
                    ),
                }
                self.last_error = self._recovery_detail["message"]
                # Persist fail-closed.
                try:
                    self.store.transition_workflow(
                        workflow_id,
                        recovery=dict(self._recovery_detail),
                        event_type="recovery_reconcile_failed",
                        event_payload=dict(self._recovery_detail),
                    )
                except StorageError as persist_exc:
                    self._recovery_detail["persist_error"] = str(persist_exc)
                    self.last_error = (
                        f"{self.last_error}; persist also failed: {persist_exc}"
                    )
                if category:
                    raise BridgeError(
                        f"{category}: {exc}",
                        category=category,
                    ) from exc
                raise BridgeError(
                    f"recovery_required: {exc}; durable ownership retained"
                ) from exc
            except Exception as exc:
                self._recovery_required = True
                category = self._classify_connect_failure(exc)
                self._recovery_detail = {
                    "reason": "recovery_reconcile_connect_failed",
                    "workflow_id": workflow_id,
                    "category": category,
                    "error": str(exc),
                }
                self.last_error = f"recovery.reconcile connect failed: {exc}"
                try:
                    self.store.transition_workflow(
                        workflow_id,
                        recovery=dict(self._recovery_detail),
                        event_type="recovery_reconcile_failed",
                        event_payload=dict(self._recovery_detail),
                    )
                except StorageError as persist_exc:
                    self._recovery_detail["persist_error"] = str(persist_exc)
                if category:
                    raise BridgeError(
                        f"{category}: {exc}",
                        category=category,
                    ) from exc
                raise BridgeError(
                    f"recovery_required: connect failed: {exc}; ownership retained"
                ) from exc

        if self.client is None or not self.connected:
            self._recovery_required = True
            raise BridgeError(
                "recovery_required: no BLE link after connect attempt; "
                "do not load/start; ownership retained"
            )

        # Query fresh state only — no load, start, or control writes.
        timeout = self.machine_info_timeout
        generation_before = self._state_notify_generation
        self._state_notify_event.clear()
        try:
            await self.client.request_status()
            await self._await_fresh_state_notification(
                generation_before=generation_before, timeout=timeout
            )
        except Exception as exc:
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "recovery_reconcile_query_failed",
                "workflow_id": workflow_id,
                "error": str(exc),
                "machine_state": self.machine_state,
                "link_kept": True,
                "message": (
                    "fresh state query failed; keep BLE for telemetry/cancel; "
                    "recovery_required; never re-load/start"
                ),
            }
            self.last_error = self._recovery_detail["message"]
            try:
                self.store.transition_workflow(
                    workflow_id,
                    recovery=dict(self._recovery_detail),
                    event_type="recovery_reconcile_failed",
                    event_payload=dict(self._recovery_detail),
                )
            except StorageError as persist_exc:
                self._recovery_detail["persist_error"] = str(persist_exc)
                self.last_error = (
                    f"{self.last_error}; persist also failed: {persist_exc}"
                )
            raise BridgeError(
                f"recovery_required: no fresh state after query ({exc}); "
                "link kept; do not load/start"
            ) from exc

        fresh = self.machine_state
        activity = self.activity

        # Loaded tea: no positive protocol marker → remain recovery_required.
        # Must run before terminal matching: idle/ready is not proof the tea
        # recipe finished (it may never have started).
        if activity == "tea" and self.phase == "loaded":
            self._recovery_required = True
            self._loaded_needs_reconcile = True
            self._recovery_detail = {
                "reason": "recovery_reconcile_tea_no_positive_marker",
                "workflow_id": workflow_id,
                "machine_state": fresh,
                "link_kept": True,
                "message": (
                    "tea loaded has no positive protocol marker; "
                    "recovery_required; do not start; never re-load"
                ),
            }
            self.last_error = self._recovery_detail["message"]
            try:
                self.store.transition_workflow(
                    workflow_id,
                    state="loaded",
                    machine_phase="loaded",
                    recovery=dict(self._recovery_detail),
                    event_type="recovery_reconcile_tea_fail_closed",
                    event_payload=dict(self._recovery_detail),
                )
            except StorageError as persist_exc:
                self._recovery_detail["persist_error"] = str(persist_exc)
                self.last_error = (
                    f"{self.last_error}; persist also failed: {persist_exc}"
                )
            raise BridgeError(
                "recovery_required: tea loaded cannot confirm recipe still loaded "
                f"(machine_state={fresh!r}; no positive protocol marker); "
                "link kept; do not start; never re-load"
            )

        # Loaded coffee: fresh armed → reattach monitoring, clear recovery after durable.
        if activity == "coffee" and fresh in _RECONCILE_LOADED_COFFEE_STATES:
            try:
                self.store.transition_workflow(
                    workflow_id,
                    state="loaded",
                    machine_phase="loaded",
                    recovery=CLEAR_RECOVERY,
                    event_type="recovery_reconciled",
                    event_payload={
                        "outcome": "loaded_armed",
                        "machine_state": fresh,
                        "connect_established": connect_established,
                    },
                )
            except StorageError as exc:
                self._recovery_required = True
                self._loaded_needs_reconcile = True
                self._recovery_detail = {
                    "reason": "recovery_reconcile_persist_failed",
                    "workflow_id": workflow_id,
                    "machine_state": fresh,
                    "error": str(exc),
                }
                self.last_error = (
                    f"fresh armed observed but durable reconcile failed: {exc}; "
                    "recovery_required (fail-closed)"
                )
                raise BridgeError(self.last_error) from exc
            self.phase = "loaded"
            self._loaded_needs_reconcile = False
            self._recovery_required = False
            self._recovery_detail = None
            self.last_error = None
            if self.connection_scope != "explicit":
                self.connection_scope = "workflow"
            result = self.status()
            result["reconciled"] = True
            result["reconcile_outcome"] = "loaded_armed"
            result["fresh_machine_state"] = fresh
            return result

        # Positive active / paused proof: reattach monitoring, do not reissue start.
        paused_report = str(self.telemetry.get("last_report") or "") in {
            "tea_paused",
            "brewer_paused",
            "grinder_paused",
        }
        if fresh in _RECONCILE_ACTIVE_STATES or (
            self.phase == "paused" and fresh in _RECONCILE_ACTIVE_STATES
        ) or (paused_report and fresh not in _RECONCILE_TERMINAL_STATES):
            if paused_report and fresh not in _RECONCILE_ACTIVE_STATES:
                new_phase = "paused"
            elif fresh == "starting":
                new_phase = "starting"
            else:
                new_phase = "running"
            try:
                self.store.transition_workflow(
                    workflow_id,
                    state=new_phase,
                    machine_phase=new_phase,
                    recovery=CLEAR_RECOVERY,
                    event_type="recovery_reconciled",
                    event_payload={
                        "outcome": new_phase,
                        "machine_state": fresh,
                        "connect_established": connect_established,
                    },
                )
            except StorageError as exc:
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "recovery_reconcile_persist_failed",
                    "workflow_id": workflow_id,
                    "machine_state": fresh,
                    "error": str(exc),
                }
                self.last_error = (
                    f"fresh active state observed but durable reconcile failed: {exc}; "
                    "recovery_required (fail-closed)"
                )
                raise BridgeError(self.last_error) from exc
            self.phase = new_phase
            self._saw_active = True
            self._recovery_required = False
            self._recovery_detail = None
            self._loaded_needs_reconcile = False
            self.last_error = None
            if self.connection_scope != "explicit":
                self.connection_scope = "workflow"
            result = self.status()
            result["reconciled"] = True
            result["reconcile_outcome"] = new_phase
            result["fresh_machine_state"] = fresh
            return result

        # Fresh confirmed terminal of an in-progress / unconfirmed workflow.
        # Loaded awaiting start is excluded: idle/ready is not proof a brew ended.
        past_loaded = self.phase not in {
            "loaded",
            "loading",
            "created",
        }
        if fresh in _RECONCILE_TERMINAL_STATES and past_loaded:
            self._finish_activity(
                str(fresh),
                release_reason="recovery_reconcile_terminal",
            )
            status = self.status()
            status["reconciled"] = True
            status["reconcile_outcome"] = "terminal"
            status["fresh_machine_state"] = fresh
            return status

        # Unknown / no positive proof: remain recovery_required; keep link.
        self._recovery_required = True
        self._recovery_detail = {
            "reason": "recovery_reconcile_unknown_state",
            "workflow_id": workflow_id,
            "machine_state": fresh,
            "activity": activity,
            "phase": self.phase,
            "link_kept": True,
            "message": (
                "fresh state does not positively prove loaded/active/terminal; "
                "recovery_required; link kept; never re-load/start"
            ),
        }
        self.last_error = self._recovery_detail["message"]
        try:
            self.store.transition_workflow(
                workflow_id,
                recovery=dict(self._recovery_detail),
                event_type="recovery_reconcile_unknown",
                event_payload=dict(self._recovery_detail),
            )
        except StorageError as persist_exc:
            self._recovery_detail["persist_error"] = str(persist_exc)
            self.last_error = (
                f"{self.last_error}; persist also failed: {persist_exc}"
            )
        raise BridgeError(
            f"recovery_required: fresh machine_state={fresh!r} is not a positive "
            "loaded/active/terminal proof; link kept; do not load/start"
        )

    def _persist_event(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
        critical: bool = False,
    ) -> dict[str, Any] | None:
        wid = workflow_id or self.active_workflow_id
        if not wid:
            return None
        try:
            return self.store.append_workflow_event(
                wid, event_type, dict(payload or {})
            )
        except StorageError as exc:
            self.last_error = f"workflow event persistence failed: {exc}"
            self._event_persist_failed = True
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "event_persist_failed",
                "workflow_id": wid,
                "event_type": event_type,
                "error": str(exc),
            }
            if critical:
                raise BridgeError(
                    f"critical workflow event persistence failed: {exc}"
                ) from exc
            return None

    def _set_workflow_state(
        self,
        state: str,
        *,
        workflow_id: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | Any = None,
        metadata: Mapping[str, Any] | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
        critical: bool = False,
    ) -> None:
        wid = workflow_id or self.active_workflow_id
        if not wid:
            return
        try:
            self.store.transition_workflow(
                wid,
                state=state,
                machine_phase=machine_phase if machine_phase is not None else state,
                recovery=recovery,
                metadata=metadata,
                event_type=event_type,
                event_payload=(
                    dict(event_payload)
                    if event_payload is not None
                    else ({"state": state} if event_type is not None else None)
                ),
            )
        except StorageError as exc:
            self.last_error = f"workflow state persistence failed: {exc}"
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "workflow_persist_failed",
                "workflow_id": wid,
                "state": state,
                "error": str(exc),
            }
            if critical:
                raise BridgeError(
                    f"critical workflow transition to {state!r} failed: {exc}"
                ) from exc

    def _workflow_public_summary(
        self, workflow_id: str | None = None
    ) -> dict[str, Any] | None:
        try:
            return self.store.workflow_summary(workflow_id)
        except StorageError:
            return None

    def _create_durable_workflow(
        self,
        *,
        kind: str,
        snapshot: Mapping[str, Any],
        state: str = "loading",
        source: str | None = None,
        owner: str | None = None,
        recipe_revision_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self.store.create_workflow_with_event(
                kind=kind,
                state=state,
                snapshot=snapshot,
                source=source or "bridge",
                owner=owner or "bridge",
                recipe_revision_id=recipe_revision_id,
                machine_phase=state,
                metadata=metadata,
                event_type="created",
                event_payload={
                    "kind": kind,
                    "state": state,
                },
            )
        except StorageError as exc:
            raise BridgeError(f"failed to create durable workflow: {exc}") from exc

    def _snapshot_coffee_recipe(self, recipe: Any, path: Path | None) -> dict[str, Any]:
        data = dict(recipe.to_dict())
        if path is not None:
            data["_source_path"] = str(path)
            data["_source_sha256"] = _sha256(path)
        return data

    def _snapshot_tea_recipe(self, recipe: Any, path: Path | None) -> dict[str, Any]:
        data = {
            "name": recipe.name,
            "kind": "tea",
            "leaf_g": recipe.leaf_g,
            "output_ml_per_steep": recipe.output_ml_per_steep,
            "pours": [
                {
                    "ml": pour.ml,
                    "temp_c": pour.temp_c,
                    "pattern": pour.pattern,
                    "pause_s": pour.pause_s,
                    "flow_ml_s": pour.flow_ml_s,
                    **({"label": pour.label} if pour.label else {}),
                }
                for pour in recipe.pours
            ],
        }
        if path is not None:
            data["_source_path"] = str(path)
            data["_source_sha256"] = _sha256(path)
        return data

    def _canonical_snapshot_content(
        self, snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Drop bridge-private underscore keys for revision hash comparison."""

        return {k: v for k, v in snapshot.items() if not str(k).startswith("_")}

    @staticmethod
    def _recipe_kind_matches_load(recipe_kind: Any, load_kind: str) -> bool:
        """True when stored recipe_kind is valid for a coffee/tea load.

        High-level storage kinds are ``coffee`` / ``tea``. Coffee content may
        also use serving kinds such as ``hot`` and ``flash-brew``.
        """

        if recipe_kind is None:
            return False
        kind_norm = str(recipe_kind).strip().lower()
        if not kind_norm:
            return False
        load = str(load_kind).strip().lower()
        if load == "tea":
            return kind_norm == "tea"
        if load == "coffee":
            return kind_norm in {
                "coffee",
                "hot",
                "ice",
                "iced",
                "flash",
                "flash-brew",
            }
        return kind_norm == load

    def _resolve_recipe_revision_id(
        self,
        params: Mapping[str, Any],
        *,
        kind: str,
        snapshot: Mapping[str, Any],
        name: str | None,
        path_provided: bool = False,
    ) -> str | None:
        rev_id = params.get("recipe_revision_id")
        canonical = self._canonical_snapshot_content(snapshot)
        expected_sha = content_sha256(canonical)
        if rev_id is not None and str(rev_id).strip():
            rid = str(rev_id).strip()
            existing = self.store.get_recipe_revision(rid)
            if existing is None:
                raise BridgeError(
                    f"unknown recipe_revision_id {rid!r}",
                    category="invalid_request",
                )
            # When a local path is also provided, revision must match the
            # validated canonical snapshot (kind + content hash) before BLE.
            if path_provided:
                recipe_kind = existing.get("recipe_kind") or (
                    (existing.get("content") or {}).get("kind")
                )
                if recipe_kind is not None and not self._recipe_kind_matches_load(
                    recipe_kind, kind
                ):
                    raise BridgeError(
                        f"recipe_revision_id {rid!r} kind {recipe_kind!r} "
                        f"does not match load kind {kind!r}",
                        category="invalid_request",
                    )
                existing_sha = existing.get("content_sha256")
                if existing_sha and existing_sha != expected_sha:
                    # Also accept direct content equality when hash encoding differs.
                    content = existing.get("content") or {}
                    if content_sha256(content) != expected_sha:
                        raise BridgeError(
                            f"recipe_revision_id {rid!r} content hash does not "
                            "match the validated local recipe snapshot",
                            category="validation_error",
                        )
            return rid
        # Path-only compatibility: best-effort durable revision (no redesign).
        try:
            recipe_row = self.store.upsert_recipe(
                kind=kind,
                name=name,
                source="bridge_load",
                provenance={"via": "bridge_load"},
            )
            rev = self.store.add_recipe_revision(
                recipe_row["recipe_id"],
                canonical,
                provenance={"source": "bridge_load"},
            )
            return str(rev["revision_id"])
        except StorageError:
            return None

    def _grinder_is_recovery(self) -> bool:
        """True only for in-progress or unreadable grinder records.

        Completed cooldown records (``in_progress=false`` with ``blocked_until``)
        intentionally persist and must not mark the daemon non-idle.
        """

        path = self.grinder_state_file
        if not path.exists():
            return False
        try:
            state = _read_json(path)
        except BridgeError:
            return True
        return bool(state.get("in_progress"))

    def recovery_record_names(self) -> list[str]:
        names: list[str] = []
        if self.coffee_state_file.exists():
            names.append(COFFEE_STATE_NAME)
        if self.tea_state_file.exists():
            names.append(TEA_STATE_NAME)
        if self._grinder_is_recovery():
            names.append(GRINDER_STATE_NAME)
        return names

    def is_idle(self) -> bool:
        """True when phase is safe, no activity, and no recovery records."""

        if self.activity is not None:
            return False
        if self.phase not in SAFE_IDLE_PHASES:
            return False
        if self.coffee_state_file.exists() or self.tea_state_file.exists():
            return False
        if self._grinder_is_recovery():
            return False
        return True

    def has_recovery_records(self) -> bool:
        return bool(self.recovery_record_names())

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
            # Fresh state-bearing notification for reconcile waiters.
            self._state_notify_generation += 1
            self._state_notify_event.set()
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

        # Persist normalized machine/telemetry for the active workflow with a
        # dense per-workflow sequence, before any terminal finalization below.
        # control_unconfirmed / stop_unconfirmed are resolvable by a later
        # confirmed telemetry terminal (coffee and tea).
        _terminal_resolvable_phases = {
            "running",
            "paused",
            "soaking",
            "starting",
            "control_unconfirmed",
            "stop_unconfirmed",
        }
        def _terminal_can_resolve(phase: str) -> bool:
            # Uncertain control/stop may be resolved by a confirmed terminal
            # even if an active-state byte was never observed (ACK loss paths).
            if phase in {"control_unconfirmed", "stop_unconfirmed"}:
                return True
            return self._saw_active

        will_finish_coffee = (
            self.activity == "coffee"
            and self.phase in _terminal_resolvable_phases
            and _terminal_can_resolve(self.phase)
            and event.state in TERMINAL_STATE_BYTES
        )
        will_finish_tea = (
            self.activity == "tea"
            and self.phase in _terminal_resolvable_phases
            and _terminal_can_resolve(self.phase)
            and event.state in TERMINAL_STATE_BYTES
        )
        if self.active_workflow_id and self.activity is not None:
            # Skip pure machine-info handshake noise once firmware is known.
            interesting = (
                event.state is not None
                or event.command_code is not None
                or dispensed is not None
                or cup_weight is not None
                or event.scale_g is not None
                or event.report_name is not None
                or event.is_error
            )
            if interesting and not (
                event.machine_info and event.state is None and event.command_code == 40521
            ):
                self._persist_event(
                    "machine",
                    {
                        "source": "telemetry",
                        **{k: v for k, v in data.items() if k != "seq"},
                        "live_seq": data.get("seq"),
                    },
                )

        if will_finish_coffee:
            self._finish_activity(event.state_name, release_reason="natural_terminal")
        if will_finish_tea:
            self._finish_activity(event.state_name, release_reason="natural_terminal")

        if (
            self.activity == "water"
            and self.phase
            in {"running", "paused", "starting", "control_unconfirmed", "stop_unconfirmed"}
            and event.command_code == REPORT_BREWER_STOP
            and (self._cleanup_task is None or self._cleanup_task.done())
        ):
            self._cleanup_task = asyncio.create_task(self._finish_natural_water())

    def _finish_activity(
        self,
        result: str,
        *,
        release_reason: str | None = None,
        emergency: bool = False,
        skip_durable_terminal: bool = False,
        request_id: str | None = None,
        idempotency_result: Mapping[str, Any] | None = None,
        **details: Any,
    ) -> None:
        previous = self.activity
        workflow_id = self.active_workflow_id
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
        if emergency:
            details["emergency"] = True
        if workflow_id:
            details.setdefault("workflow_id", workflow_id)

        # Confirmed terminal/cancel/stop must persist final workflow state/event
        # (and matching request completion when provided) before close_session +
        # disconnect. Natural terminals have no request_id. One transaction.
        # Prior machine-event persistence gaps remain observable and block a
        # fully durable release claim even if the terminal row commits.
        event_gap = bool(self._event_persist_failed)
        durable_ok = True
        if workflow_id and not skip_durable_terminal:
            try:
                self.store.commit_workflow_terminal(
                    workflow_id,
                    state=str(result),
                    event_type="terminal",
                    event_payload={
                        "result": result,
                        "activity": previous,
                        "release_reason": release_reason,
                        **{
                            key: value
                            for key, value in details.items()
                            if key not in {"workflow_id"}
                        },
                    },
                    machine_phase=str(result),
                    recovery=(
                        {
                            "reason": "event_persist_failed_before_terminal",
                            "machine_result": result,
                        }
                        if event_gap
                        else CLEAR_RECOVERY
                    ),
                    request_id=request_id,
                    idempotency_result=(
                        dict(idempotency_result)
                        if idempotency_result is not None
                        else None
                    ),
                )
            except StorageError as exc:
                durable_ok = False
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "terminal_persist_failed",
                    "workflow_id": workflow_id,
                    "machine_result": result,
                    "request_id": request_id,
                    "error": str(exc),
                }
                self.last_error = (
                    f"machine terminal confirmed but durable commit failed: {exc}; "
                    "BLE release withheld; recovery_required"
                )
                self.last_operation = {
                    "activity": previous,
                    "result": result,
                    "finished_at": round(time.time(), 3),
                    "persistence_failed": True,
                    **details,
                }
                # Keep workflow identity and activity linkage for recovery.
                self.phase = "recovery_required"
                return
            if event_gap:
                durable_ok = False
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "event_persist_failed_before_terminal",
                    "workflow_id": workflow_id,
                    "machine_result": result,
                }
                self.last_error = (
                    "durable terminal committed but earlier machine event "
                    "persistence failed; BLE release withheld; recovery_required"
                )

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
        self.active_workflow_id = None
        self.phase = (
            "recovery_required"
            if not durable_ok
            else ("idle" if self.connected else "disconnected")
        )
        self.targets = {}
        self._saw_active = False
        self._cup_baseline_g = None
        if durable_ok:
            self._recovery_required = False
            self._recovery_detail = None
            self._event_persist_failed = False
        # Prompt release only after fully durable terminal commit succeeds.
        if release_reason is not None and durable_ok:
            self._schedule_auto_release(release_reason)
        # Arm/clear orphan idle fallback after activity settles (lifecycle only).
        self._arm_or_clear_idle_orphan_watch()

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
                release_reason="natural_terminal",
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
        recovery_records = self.recovery_record_names()
        workflow_summary = self._workflow_public_summary(self.active_workflow_id)
        if workflow_summary is None:
            workflow_summary = self._workflow_public_summary()
        recovery_state: dict[str, Any] | None = None
        if self._recovery_required or self._recovery_detail:
            recovery_state = {
                "required": bool(self._recovery_required),
                "detail": dict(self._recovery_detail or {}),
            }
        elif self.phase in {"control_unconfirmed", "stop_unconfirmed", "recovery_required"}:
            recovery_state = {
                "required": True,
                "detail": {
                    "phase": self.phase,
                    "workflow_id": self.active_workflow_id,
                },
            }
        return {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "rpc_protocol_min": RPC_PROTOCOL_MIN,
            "rpc_protocol_max": RPC_PROTOCOL_MAX,
            "rpc_protocol_current": RPC_PROTOCOL_CURRENT,
            "record_format_version": BRIDGE_RECORD_FORMAT_VERSION,
            "instance_id": self.instance_id,
            "core_version": self.core_version,
            "config_fingerprint": self.config_fingerprint,
            "started_at": self.started_at,
            "pid": os.getpid(),
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
            "connection_scope": self.connection_scope,
            "release_pending": self.release_pending,
            "last_disconnect_reason": self.last_disconnect_reason,
            "last_disconnect_time": self.last_disconnect_time,
            "last_disconnect_error": self.last_disconnect_error,
            # Idle orphan fallback observability (read-only; does not arm/reset).
            "idle_disconnect_s": self._idle_disconnect_seconds(),
            "idle_orphan_since": self._idle_orphan_since,
            "idle_orphan_deadline": self._idle_orphan_deadline,
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
            "idle": self.is_idle(),
            "active_workflow_id": self.active_workflow_id,
            "workflow": workflow_summary,
            "recovery": recovery_state,
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

    def events_since(
        self,
        since: int = 0,
        *,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Return events. Prefer durable per-workflow cursor when workflow_id set.

        Live ring-buffer events remain available without workflow_id for
        compatibility. status/events never initiate BLE.
        """

        since = max(0, int(since))
        if workflow_id:
            page = self.store.list_workflow_events_page(
                str(workflow_id), since_seq=since
            )
            # Also include matching live telemetry for the active workflow when
            # the durable cursor is caught up (no artificial gap from mixing).
            return {
                "workflow_id": page["workflow_id"],
                "events": page["events"],
                "next_since": page["next_since"],
                "gap_detected": bool(page.get("gap_detected")),
                "gap_reason": page.get("gap_reason"),
                "max_seq": page.get("max_seq"),
                "source": "durable",
            }
        events = [event for event in self._events if int(event["seq"]) > since]
        # Live ring buffer may drop old events; surface gap when since is behind
        # the oldest retained sequence.
        gap_detected = False
        gap_reason: str | None = None
        if self._events and since > 0:
            oldest = int(self._events[0]["seq"])
            if since + 1 < oldest and since < self._event_seq:
                gap_detected = True
                gap_reason = "live_ring_buffer_evicted"
        return {
            "events": events,
            "next_since": self._event_seq,
            "gap_detected": gap_detected,
            "gap_reason": gap_reason,
            "source": "live",
            "workflow_id": self.active_workflow_id,
        }

    async def _resolve_address(self, requested: str | None, timeout: float) -> tuple[str, str]:
        address = requested or self.default_address
        if address:
            return str(address), "xBloom Studio"
        devices = await self.scan_fn(timeout=float(timeout))
        if len(devices) != 1:
            raise BridgeError(f"expected exactly one nearby xBloom; found {len(devices)}")
        device = devices[0]
        return str(device.address), getattr(device, "name", None) or "xBloom Studio"

    def _auto_release_scopes(self) -> frozenset[str]:
        return frozenset({"workflow", "one-shot"})

    def _idle_disconnect_seconds(self) -> float:
        """Configured orphan idle fallback seconds; 0 disables. Default 300."""

        raw = self.environ.get("XBLOOM_BRIDGE_IDLE_DISCONNECT_S")
        if raw is None or str(raw).strip() == "":
            return 300.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 300.0

    def _orphan_idle_eligible(self) -> bool:
        """True only for leftover auto-owned links with no activity/workflow.

        Explicit debug connections, any in-flight activity, active/recovery
        durable workflows, and unconfirmed control phases are never eligible.
        """

        if not self.connected:
            return False
        if self.connection_scope not in self._auto_release_scopes():
            return False
        if self.release_pending:
            return False
        if self.activity is not None:
            return False
        if self.active_workflow_id is not None:
            return False
        if self._recovery_required:
            return False
        # Loaded/running/paused/recovery/unconfirmed phases must never time out
        # even if activity were cleared incorrectly.
        protected_phases = {
            "loaded",
            "loading",
            "running",
            "paused",
            "soaking",
            "starting",
            "stopping",
            "writing",
            "recovery",
            "recovering",
            "recovery_required",
            "control_unconfirmed",
            "stop_unconfirmed",
            "load_unconfirmed",
            "write_unconfirmed",
        }
        if self.phase in protected_phases:
            return False
        try:
            if self.store.get_active_workflow() is not None:
                return False
        except StorageError:
            # Conservative: do not orphan-timeout when durable state is unreadable.
            return False
        return True

    def _cancel_idle_orphan_task(self) -> None:
        task = self._idle_orphan_task
        self._idle_orphan_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _arm_or_clear_idle_orphan_watch(self) -> None:
        """Arm or clear the orphan idle timer from lifecycle transitions only.

        Must never be called from ``status`` / ``events`` (those must not
        create, reset, or extend the timer).
        """

        timeout = self._idle_disconnect_seconds()
        if timeout <= 0 or not self._orphan_idle_eligible():
            self._idle_orphan_since = None
            self._idle_orphan_deadline = None
            self._cancel_idle_orphan_task()
            return
        if self._idle_orphan_since is not None:
            # Already armed for this leftover episode; do not extend.
            return
        now = time.time()
        self._idle_orphan_since = now
        self._idle_orphan_deadline = now + timeout
        self._cancel_idle_orphan_task()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run() -> None:
            try:
                await asyncio.sleep(timeout)
                async with self._op_lock:
                    if not self._orphan_idle_eligible():
                        self._idle_orphan_since = None
                        self._idle_orphan_deadline = None
                        return
                    await self._disconnect_unlocked(
                        reason="idle_orphan_disconnect",
                        require_idle_activity=True,
                        record_failure=True,
                    )
                    self._idle_orphan_since = None
                    self._idle_orphan_deadline = None
            except asyncio.CancelledError:
                return
            finally:
                if asyncio.current_task() is self._idle_orphan_task:
                    self._idle_orphan_task = None

        self._idle_orphan_task = loop.create_task(_run())

    def _cancel_pending_release(self) -> None:
        self.release_pending = False
        self._pending_release_reason = None
        task = self._release_task
        self._release_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _schedule_auto_release(self, reason: str) -> None:
        """Queue a prompt BLE release that waits for ``_op_lock`` (race-safe).

        Safe to call from the BLE event path while a control RPC holds the lock:
        the task only disconnects after the in-flight write finishes. Explicit
        debug connections (``connection_scope == "explicit"``) are never
        auto-released. Prompt terminal release is immediate; the idle timer is
        only an orphan fallback.
        """

        if self.connection_scope not in self._auto_release_scopes():
            return
        if not self.connected:
            return
        if self.release_pending:
            # Keep the first pending reason; a later natural terminal during
            # cancel cleanup is still a single release.
            return
        # Prompt release owns the link; clear any orphan idle arm.
        self._idle_orphan_since = None
        self._idle_orphan_deadline = None
        self._cancel_idle_orphan_task()
        self.release_pending = True
        self._pending_release_reason = reason
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._release_task = loop.create_task(self._run_scheduled_release())

    async def _run_scheduled_release(self) -> None:
        try:
            async with self._op_lock:
                if not self.release_pending:
                    return
                # A newer workflow may have claimed the link before this task ran.
                if self.activity is not None:
                    self.release_pending = False
                    self._pending_release_reason = None
                    self._arm_or_clear_idle_orphan_watch()
                    return
                reason = self._pending_release_reason or "auto_release"
                await self._disconnect_unlocked(
                    reason=reason,
                    require_idle_activity=True,
                    record_failure=True,
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # Never erase a confirmed terminal result; surface disconnect only.
            self.last_disconnect_error = str(exc)
            self.last_disconnect_time = round(time.time(), 3)
            if self.last_disconnect_reason is None:
                self.last_disconnect_reason = self._pending_release_reason
            self.release_pending = False
            self._pending_release_reason = None
            # Disconnect failed while ownership may still be held: arm fallback.
            self._arm_or_clear_idle_orphan_watch()
        finally:
            if asyncio.current_task() is self._release_task:
                self._release_task = None

    @staticmethod
    def _classify_connect_failure(exc: BaseException) -> str | None:
        """Map transport errors to a stable external-busy category when applicable.

        Bleak/OS text often cannot distinguish unavailable vs already-connected
        vs GATT busy. When any of those signals appear, return
        ``device_busy_external`` without claiming a more specific root cause.
        """

        text = str(exc).casefold()
        if not text:
            return None
        for marker in _DEVICE_BUSY_EXTERNAL_MARKERS:
            if marker in text:
                return "device_busy_external"
        # Common exception type names from Bleak / OS stacks.
        name = type(exc).__name__.casefold()
        if any(
            token in name
            for token in ("bleak", "bluetooth", "gatt", "busy", "timeout")
        ):
            return "device_busy_external"
        return None

    def _bind_client_listeners(self, client: Any) -> None:
        """Register event + disconnect observers for a newly owned client."""

        self._client_generation += 1
        self._bound_client_generation = self._client_generation
        client.add_event_listener(self._on_event)
        add_disc = getattr(client, "add_disconnect_listener", None)
        if callable(add_disc):
            add_disc(self._on_client_disconnected)

    def _unbind_client_listeners(self, client: Any, *, expected: bool = True) -> None:
        """Detach observers; mark disconnect expected before bridge-initiated close."""

        # Invalidate in-flight unexpected callbacks for this generation.
        self._bound_client_generation = 0
        try:
            client.remove_event_listener(self._on_event)
        except Exception:
            pass
        remove_disc = getattr(client, "remove_disconnect_listener", None)
        if callable(remove_disc):
            try:
                remove_disc(self._on_client_disconnected)
            except Exception:
                pass
        if expected:
            mark = getattr(client, "mark_disconnect_expected", None)
            if callable(mark):
                try:
                    mark()
                except Exception:
                    pass

    def _on_client_disconnected(self, expected: bool = False) -> None:
        """Client disconnect listener entrypoint (expected vs unexpected)."""

        if expected:
            # Bridge-initiated close/disconnect must never create recovery.
            return
        # Stale callback after intentional unbind / terminal release.
        if self._bound_client_generation == 0:
            return
        self._handle_unexpected_ble_disconnect()

    def _handle_unexpected_ble_disconnect(self) -> None:
        """Detach ownership on an unexpected BLE drop; never auto-reconnect.

        With an active durable workflow, retain activity and workflow identity,
        persist ``ble_disconnected`` recovery, and surface recovery_required.
        With no activity/workflow, settle to disconnected without inventing recovery.
        Recovery may only reconnect/query/reconcile later via explicit RPC —
        never load, start, or other uncertain machine actions here.
        """

        client = self.client
        # Preserve address for explicit recovery reconnect.
        preserved_address = self.address or self.default_address
        generation = self._bound_client_generation
        self._bound_client_generation = 0
        if client is not None:
            try:
                client.remove_event_listener(self._on_event)
            except Exception:
                pass
            remove_disc = getattr(client, "remove_disconnect_listener", None)
            if callable(remove_disc):
                try:
                    remove_disc(self._on_client_disconnected)
                except Exception:
                    pass
        # Drop stale client ownership; do not clear workflow activity.
        self.client = None
        self.connection_scope = None
        self.release_pending = False
        self._pending_release_reason = None
        self._cancel_pending_release()
        self._idle_orphan_since = None
        self._idle_orphan_deadline = None
        self._cancel_idle_orphan_task()
        if preserved_address:
            self.address = preserved_address
            if not self.default_address:
                self.default_address = preserved_address
        self.machine_name = None
        self.last_disconnect_reason = "ble_disconnected"
        self.last_disconnect_time = round(time.time(), 3)
        self.last_disconnect_error = None

        has_workflow = self.active_workflow_id is not None or self.activity is not None
        if not has_workflow:
            # Idle / no durable ownership: settle without inventing recovery.
            if self.phase not in {"disconnected", "idle"}:
                self.phase = "disconnected"
            elif self.phase == "idle":
                self.phase = "disconnected"
            return

        # Active durable workflow / activity: recovery_required, no auto-reconnect.
        prior_phase = self.phase
        self._recovery_required = True
        recovery_payload: dict[str, Any] = {
            "reason": "ble_disconnected",
            "workflow_id": self.active_workflow_id,
            "activity": self.activity,
            "phase": prior_phase,
            "address": preserved_address,
            "client_generation": generation,
            "message": (
                "unexpected BLE disconnect; recovery may reconnect and query only; "
                "do not repeat load/start or other uncertain machine actions"
            ),
        }
        # Loaded coffee remains a loaded recovery candidate needing fresh armed.
        if prior_phase == "loaded" and self.activity == "coffee":
            self._loaded_needs_reconcile = True
            self.phase = "loaded"
            recovery_payload["loaded_needs_reconcile"] = True
        elif prior_phase == "loaded" and self.activity == "tea":
            # Tea has no positive loaded marker — fail-closed.
            self._loaded_needs_reconcile = True
            self.phase = "loaded"
            recovery_payload["tea_fail_closed"] = True
        elif prior_phase in {
            "running",
            "paused",
            "soaking",
            "starting",
            "control_unconfirmed",
            "stop_unconfirmed",
            "stopping",
            "writing",
            "load_unconfirmed",
            "write_unconfirmed",
        }:
            # Become recovery_required without rewriting phase back to running.
            # Keep the prior phase for observability; recovery flag is authoritative.
            self.phase = prior_phase
        else:
            # created/loading/recovery_required/etc.: keep phase, require recovery.
            self.phase = prior_phase if prior_phase else "recovery_required"

        self._recovery_detail = recovery_payload
        self.last_error = recovery_payload["message"]

        # Persist ble_disconnected event + recovery state; fail closed in memory.
        wid = self.active_workflow_id
        if wid:
            try:
                self.store.transition_workflow(
                    wid,
                    # Do not force durable state to recovery_required for
                    # running/paused — keep prior non-terminal state when active.
                    state=(
                        prior_phase
                        if prior_phase
                        in {
                            "loaded",
                            "running",
                            "paused",
                            "soaking",
                            "starting",
                            "control_unconfirmed",
                            "stop_unconfirmed",
                            "created",
                            "loading",
                        }
                        else "recovery_required"
                    ),
                    machine_phase=prior_phase,
                    recovery=recovery_payload,
                    event_type="ble_disconnected",
                    event_payload=dict(recovery_payload),
                )
            except StorageError as exc:
                self._recovery_required = True
                self._recovery_detail = {
                    **recovery_payload,
                    "persist_error": str(exc),
                    "reason": "ble_disconnected_persist_failed",
                }
                self.last_error = (
                    f"unexpected BLE disconnect and recovery persist failed: {exc}; "
                    "recovery_required (fail-closed in memory)"
                )

    async def _connect_unlocked(
        self,
        params: Mapping[str, Any],
        *,
        scope: str = "explicit",
    ) -> dict[str, Any]:
        requested = params.get("address")
        if self.connected:
            if requested and str(requested).casefold() != str(self.address).casefold():
                raise BridgeError("bridge already owns a different xBloom connection")
            # Explicit debug connect upgrades scope so auto-release is suppressed.
            if scope == "explicit":
                self.connection_scope = "explicit"
                self._cancel_pending_release()
                # Upgrading an orphan auto-owned link must clear idle timeout so
                # status truthfully shows no disconnect deadline under explicit.
                self._idle_orphan_since = None
                self._idle_orphan_deadline = None
                self._cancel_idle_orphan_task()
            return self.status()
        self._cancel_pending_release()
        # Preserve workflow phase during recovery reconnect; only show connecting
        # when there is no durable activity to keep.
        prior_phase = self.phase
        if self.activity is None and self.active_workflow_id is None:
            self.phase = "connecting"
        self.last_error = None
        self._machine_info_ready.clear()
        try:
            address, name = await self._resolve_address(
                str(requested) if requested else None,
                float(params.get("scan_timeout", 8.0)),
            )
        except Exception as exc:
            if self.activity is None and self.active_workflow_id is None:
                self.phase = "disconnected"
            category = self._classify_connect_failure(exc)
            if category:
                raise BridgeError(
                    f"{category}: {exc}",
                    category=category,
                ) from exc
            raise
        client = self.client_factory(address)
        try:
            # One connect attempt only — no retry / preemption / background reconnect.
            # Bind disconnect listeners only after the link is up so a connect-time
            # failure cannot race as unexpected recovery against a half-owned client.
            await client.connect()
            if not bool(getattr(client, "is_connected", False)):
                raise BridgeError(f"failed to connect to {address}")
            # Ownership + listeners are established together before any further await.
            self.client = client
            self.address = address
            self.machine_name = name
            self.connection_scope = scope
            self._bind_client_listeners(client)
            await client.open_session()
            if self.activity is None and self.active_workflow_id is None:
                self.phase = "idle"
            # else: keep prior recovery/workflow phase; event listener reattached.
            if not self.connected:
                # Unexpected drop between bind and open_session completion.
                raise BridgeError(
                    self.last_error
                    or "BLE link lost during session open; recovery_required"
                )
            await client.request_status()
            try:
                await asyncio.wait_for(
                    self._machine_info_ready.wait(), timeout=self.machine_info_timeout
                )
            except asyncio.TimeoutError:
                self.last_error = "machine-info report not observed; writes remain gated"
        except Exception as exc:
            # Unexpected drop may already have unbound and cleared self.client.
            if self.client is client:
                self._unbind_client_listeners(client, expected=True)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self.client = None
            elif client is not None:
                # Never owned / already detached by disconnect callback: best-effort
                # close without inventing a second recovery transition.
                mark = getattr(client, "mark_disconnect_expected", None)
                if callable(mark):
                    try:
                        mark()
                    except Exception:
                        pass
                try:
                    await client.disconnect()
                except Exception:
                    pass
            self.connection_scope = None
            if self.activity is None and self.active_workflow_id is None:
                self.phase = "disconnected"
            else:
                # Preserve recovery phase if drop handler already recorded it.
                if not self._recovery_required:
                    self.phase = prior_phase
            self._arm_or_clear_idle_orphan_watch()
            category = self._classify_connect_failure(exc)
            if category:
                self.last_error = f"{category}: {exc}"
                raise BridgeError(
                    f"{category}: connect failed ({exc}); one attempt only, no retry",
                    category=category,
                ) from exc
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError(str(exc)) from exc
        # Lifecycle transition only (not status/events): arm if already orphan.
        self._arm_or_clear_idle_orphan_watch()
        return self.status()

    async def _disconnect_unlocked(
        self,
        *,
        reason: str = "explicit",
        require_idle_activity: bool = True,
        record_failure: bool = True,
        clear_address: bool = True,
    ) -> dict[str, Any]:
        if require_idle_activity and self.activity is not None:
            raise BridgeError("an activity is loaded or running; stop/cancel it first")
        client = self.client
        if client is None:
            self.connection_scope = None
            self.release_pending = False
            self._pending_release_reason = None
            self.phase = "disconnected"
            self._idle_orphan_since = None
            self._idle_orphan_deadline = None
            self._cancel_idle_orphan_task()
            return self.status()
        # Unbind listeners and mark expected *before* close so Bleak callbacks
        # cannot race after terminal release and invent recovery.
        self._unbind_client_listeners(client, expected=True)
        disconnect_error: str | None = None
        try:
            try:
                await client.close_session()
            except Exception as exc:
                disconnect_error = f"close_session failed: {exc}"
            try:
                await client.disconnect()
            except Exception as exc:
                detail = f"disconnect failed: {exc}"
                disconnect_error = (
                    f"{disconnect_error}; {detail}" if disconnect_error else detail
                )
        finally:
            # Always drop ownership after a release attempt so the next workflow
            # can reconnect. Physical retry of machine actions is never done here.
            # Release never auto-reconnects.
            self.client = None
            if clear_address:
                self.address = None
            self.machine_name = None
            self.connection_scope = None
            self.phase = "disconnected"
            # Drop last machine_state so a later workflow can pass idle preflight
            # after a confirmed release (fresh link will re-observe reports).
            self.machine_state = None
            self.release_pending = False
            self._pending_release_reason = None
            self._idle_orphan_since = None
            self._idle_orphan_deadline = None
            self._cancel_idle_orphan_task()
            self.last_disconnect_reason = reason
            self.last_disconnect_time = round(time.time(), 3)
            if disconnect_error and record_failure:
                self.last_disconnect_error = disconnect_error
            else:
                self.last_disconnect_error = None
        return self.status()

    async def _ensure_connected(
        self,
        params: Mapping[str, Any],
        *,
        scope: str = "one-shot",
    ) -> bool:
        """Connect if needed. Returns True when this call established the link."""

        if self.connected:
            return False
        await self._connect_unlocked(params, scope=scope)
        return True

    async def _release_auto_connect_on_preflight_failure(self, newly_connected: bool) -> None:
        """Drop a connection that only exists because this op auto-connected.

        Never tears down an explicit debug connection or a pre-existing link.
        """

        if not newly_connected:
            return
        if self.activity is not None:
            return
        if not self.connected:
            return
        if self.connection_scope == "explicit":
            return
        await self._disconnect_unlocked(
            reason="preflight_or_load_failed",
            require_idle_activity=True,
            record_failure=True,
        )

    async def _prompt_release_auto_owned(self, reason: str) -> None:
        """Prompt-release one-shot/workflow auto-owned links under ``_op_lock``.

        Used by read-only one-shots after success or failure. Never releases an
        explicit debug connection, an active activity, or a durable workflow.
        """

        if self.activity is not None:
            return
        if self.active_workflow_id is not None:
            return
        if self.connection_scope not in self._auto_release_scopes():
            return
        if not self.connected:
            return
        await self._disconnect_unlocked(
            reason=reason,
            require_idle_activity=True,
            record_failure=True,
        )

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

    async def _probe(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """One-shot redacted machine probe; never a durable workflow.

        Rejects an existing active/recovery workflow *before* any new BLE
        connection attempt. Connects as one-shot when needed, awaits a fresh
        state-bearing response where claimed, prompt-releases auto-owned links
        on success or failure, and retains an existing explicit debug connection.
        """

        # Fail closed before connect: active activity, recovery flag, or durable
        # non-terminal workflow must not be probed over a new link.
        if self.activity is not None:
            raise BridgeError(
                f"bridge is busy with {self.activity}:{self.phase}; "
                "cancel or wait before probing"
            )
        if self._recovery_required:
            raise BridgeError(
                "recovery_required; resolve recovery before probing"
            )
        try:
            active_wf = self.store.get_active_workflow()
        except StorageError as exc:
            # Fail closed: unreadable durable state must never open a new BLE link.
            raise BridgeError(
                f"durable workflow state unreadable; refuse probe connect: {exc}",
                category="durable_state_unreadable",
            ) from exc
        if active_wf is not None:
            raise BridgeError(
                f"active durable workflow {active_wf.get('workflow_id')!r} "
                f"(state={active_wf.get('state')!r}); cancel or wait before probing"
            )

        newly = await self._ensure_connected(params, scope="one-shot")
        try:
            if self.client is None or not self.connected:
                raise BridgeError("probe requires a BLE connection")
            info = _public_machine_info(await self.client.read_machine_info())
            self.machine_info.update(info)
            # Require a *fresh* state-bearing notification after status query.
            generation_before = self._state_notify_generation
            self._state_notify_event.clear()
            state_fresh = False
            state_unconfirmed_reason: str | None = None
            try:
                await self.client.request_status()
                await self._await_fresh_state_notification(
                    generation_before=generation_before,
                    timeout=self.machine_info_timeout,
                )
                state_fresh = True
            except Exception as exc:
                state_unconfirmed_reason = str(exc)
            result: dict[str, Any] = {
                "command": "probe",
                "status": "ok",
                "connected": self.connected,
                "connection_scope": self.connection_scope,
                "firmware": info.get("firmware"),
                "machine_info": info,
                "settings": self._settings_view(info),
                "vendor_service": True,
                "brew_control_sent": False,
                "read_only": True,
                "newly_connected": newly,
                "machine_state_fresh": state_fresh,
            }
            if state_fresh:
                result["machine_state"] = self.machine_state
            else:
                # Do not present stale machine_state as fresh.
                result["machine_state"] = None
                result["machine_state_unconfirmed"] = True
                if state_unconfirmed_reason:
                    result["machine_state_unconfirmed_reason"] = state_unconfirmed_reason
            return result
        finally:
            await self._prompt_release_auto_owned("probe_done")

    async def _settings_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only one-shot: no durable workflow; prompt-release auto-owned link."""

        self._ensure_no_loaded_record()
        await self._ensure_connected(params, scope="one-shot")
        try:
            self._require_idle_operation()
            info = _public_machine_info(await self.client.read_machine_info())
            self.machine_info.update(info)
            return {
                "settings": self._settings_view(info),
                "read_only": True,
                "firmware": info.get("firmware"),
            }
        finally:
            # Success or failure: drop one-shot/workflow auto-owned links only.
            # Explicit debug connections are retained.
            await self._prompt_release_auto_owned("settings_read_done")

    async def _settings_write(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        cached = self._idempotency_preflight(
            "settings.write", params, workflow_id=None
        )
        if cached is not None:
            return cached
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
        if self.active_workflow_id is not None and self.activity is not None:
            raise BridgeError(
                f"active workflow {self.active_workflow_id} still owns the bridge"
            )
        reserved = self._reserve_request(
            "settings.write", params, workflow_id=None
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        # Pre-write zone: connect / baseline / durable create. No machine write yet.
        # Failures are retryable (IDEM_FAILED) and must prompt-release auto-owned
        # links (including pre-existing orphans), never wedge request_id pending.
        before: dict[str, Any] = {}
        firmware = ""
        wf: dict[str, Any]
        try:
            await self._ensure_connected(params, scope="one-shot")
            firmware = self._require_idle_write_preflight()
            before_info = _public_machine_info(await self.client.read_machine_info())
            before = self._settings_view(before_info)
            if any(before.get(key) is None for key in requested):
                raise BridgeError(
                    "cannot safely write settings without a complete 40521 baseline"
                )
            snapshot = {
                "kind": "settings",
                "requested": dict(requested),
                "before": {key: before[key] for key in requested},
            }
            wf = self._create_durable_workflow(
                kind="settings",
                snapshot=snapshot,
                state="writing",
                source=str(params.get("source") or "bridge"),
                metadata={
                    "requested": dict(requested),
                    "machine_address": self.address,
                },
            )
        except BaseException as exc:
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._prompt_release_auto_owned("settings_write_preflight_failed")
            raise
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id
        self.activity = "settings"
        self.phase = "writing"
        if self.connection_scope != "explicit":
            self.connection_scope = "one-shot"
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
            error = (
                f"settings write failed; rollback_confirmed={rollback_ok}: {exc}"
            )
            if rollback_ok:
                self._fail_request(request_id, error, keep_pending=False)
                self._finish_activity(
                    "write_failed_rolled_back",
                    release_reason="settings_write_failed_rollback",
                    rollback_confirmed=True,
                    error=str(exc),
                )
                raise BridgeError(error) from exc
            self.phase = "control_unconfirmed"
            self.last_error = error
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={
                    "reason": "control_unconfirmed",
                    "request_id": request_id,
                    "error": str(exc),
                    "rollback_confirmed": False,
                },
                event_type="control_unconfirmed",
                event_payload={
                    "error": str(exc),
                    "rollback_confirmed": False,
                },
            )
            self._fail_request(request_id, error, keep_pending=True)
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "control_unconfirmed",
                "request_id": request_id,
                "workflow_id": workflow_id,
            }
            raise BridgeError(
                f"{error}; recovery_required; do not retry the machine write"
            ) from exc
        self.machine_info.update(readback_info)
        result = {
            "status": "written_and_read_back",
            "firmware": firmware,
            "before": {key: before[key] for key in requested},
            "requested": dict(requested),
            "readback": {key: readback_info[key] for key in requested},
            "protocol_source": "Android APK commands 8005/8010/4508/8103",
            "hardware_write_tested_by_project": False,
            "workflow_id": workflow_id,
            "kind": "settings",
            "snapshot_sha256": wf.get("snapshot_sha256"),
        }
        self._finish_activity(
            "written_and_read_back",
            release_reason="settings_write_complete",
            request_id=request_id,
            idempotency_result=result,
        )
        # Durable terminal/idempotency commit may roll back after a confirmed
        # machine write: never claim success or release; keep pending + ownership.
        if self.phase == "recovery_required" or self._recovery_required:
            raise BridgeError(
                self.last_error
                or (
                    "settings write confirmed on machine but durable terminal "
                    "commit failed; recovery_required; do not retry the write"
                )
            )
        return result

    async def _advanced_read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Read-only one-shot: no durable workflow; prompt-release auto-owned link."""

        self._ensure_no_loaded_record()
        await self._ensure_connected(params, scope="one-shot")
        try:
            self._require_idle_operation()
            info = _public_machine_info(await self.client.read_machine_info())
            values = await self.client.read_advanced_settings()
            self.machine_info.update(info)
            return {
                "settings": self._advanced_levels(values, info),
                "read_only": True,
                "firmware": info.get("firmware"),
            }
        finally:
            await self._prompt_release_auto_owned("advanced_read_done")

    async def _advanced_write(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        cached = self._idempotency_preflight(
            "advanced.write", params, workflow_id=None
        )
        if cached is not None:
            return cached
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
        if self.active_workflow_id is not None and self.activity is not None:
            raise BridgeError(
                f"active workflow {self.active_workflow_id} still owns the bridge"
            )
        reserved = self._reserve_request(
            "advanced.write", params, workflow_id=None
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        # Pre-write zone: connect / baseline / durable create. Retryable on failure.
        info: dict[str, Any] = {}
        before: dict[str, Any] = {}
        radius_target: int | None = None
        vibration_target: int | None = None
        expected: dict[str, Any] = {}
        firmware = ""
        wf: dict[str, Any]
        try:
            await self._ensure_connected(params, scope="one-shot")
            firmware = self._require_idle_write_preflight()
            info = _public_machine_info(await self.client.read_machine_info())
            before = await self.client.read_advanced_settings()
            if radius_level is not None:
                baseline = info.get("pouring_radius_init")
                if not isinstance(baseline, int) or not 560 <= baseline <= 840:
                    raise BridgeError(
                        "machine did not expose a safe pour-radius baseline "
                        "(expected 560-840)"
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
            snapshot = {
                "kind": "advanced",
                "requested_levels": {
                    key: value
                    for key, value in {
                        "pour_radius_level": (
                            int(radius_level) if radius_level is not None else None
                        ),
                        "vibration_level": (
                            int(vibration_level)
                            if vibration_level is not None
                            else None
                        ),
                    }.items()
                    if value is not None
                },
                "expected": dict(expected),
                "before": dict(before),
            }
            wf = self._create_durable_workflow(
                kind="advanced",
                snapshot=snapshot,
                state="writing",
                source=str(params.get("source") or "bridge"),
                metadata={
                    "expected": dict(expected),
                    "machine_address": self.address,
                },
            )
        except BaseException as exc:
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._prompt_release_auto_owned("advanced_write_preflight_failed")
            raise
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id
        self.activity = "advanced"
        self.phase = "writing"
        if self.connection_scope != "explicit":
            self.connection_scope = "one-shot"
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
                raise BridgeError(
                    f"advanced-settings readback mismatch: {mismatches}"
                )
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
            error = (
                f"advanced-settings write failed; "
                f"rollback_confirmed={rollback_ok}: {exc}"
            )
            if rollback_ok:
                self._fail_request(request_id, error, keep_pending=False)
                self._finish_activity(
                    "write_failed_rolled_back",
                    release_reason="advanced_write_failed_rollback",
                    rollback_confirmed=True,
                    error=str(exc),
                )
                raise BridgeError(error) from exc
            self.phase = "control_unconfirmed"
            self.last_error = error
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={
                    "reason": "control_unconfirmed",
                    "request_id": request_id,
                    "error": str(exc),
                    "rollback_confirmed": False,
                },
                event_type="control_unconfirmed",
                event_payload={
                    "error": str(exc),
                    "rollback_confirmed": False,
                },
            )
            self._fail_request(request_id, error, keep_pending=True)
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "control_unconfirmed",
                "request_id": request_id,
                "workflow_id": workflow_id,
            }
            raise BridgeError(
                f"{error}; recovery_required; do not retry the machine write"
            ) from exc
        self.machine_info.update(info)
        result = {
            "status": "written_and_read_back",
            "firmware": firmware,
            "before": self._advanced_levels(before, info),
            "readback": self._advanced_levels(readback, info),
            "protocol_source": "Android APK CodeModule2 commands 11506-11509",
            "hardware_write_tested_by_project": False,
            "workflow_id": workflow_id,
            "kind": "advanced",
            "snapshot_sha256": wf.get("snapshot_sha256"),
        }
        self._finish_activity(
            "written_and_read_back",
            release_reason="advanced_write_complete",
            request_id=request_id,
            idempotency_result=result,
        )
        if self.phase == "recovery_required" or self._recovery_required:
            raise BridgeError(
                self.last_error
                or (
                    "advanced write confirmed on machine but durable terminal "
                    "commit failed; recovery_required; do not retry the write"
                )
            )
        return result

    async def _coffee_load(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        # Completed/pending exact duplicates before any mutable gate / BLE.
        cached = self._idempotency_preflight(
            "coffee.load", params, workflow_id=None
        )
        if cached is not None:
            return cached
        self._ensure_no_loaded_record()
        if self.active_workflow_id is not None and self.activity is not None:
            raise BridgeError(
                f"active workflow {self.active_workflow_id} still owns the bridge"
            )
        raw_path = params.get("recipe")
        rev_param = params.get("recipe_revision_id")
        has_path = raw_path is not None and str(raw_path).strip() != ""
        has_rev = rev_param is not None and str(rev_param).strip() != ""
        if not has_path and not has_rev:
            raise BridgeError(
                "coffee.load requires a local recipe path or recipe_revision_id",
                category="invalid_request",
            )
        from xbloom_safety import (
            SafetyError,
            load_strict_recipe,
            recipe_summary,
            strict_validate,
        )
        from .recipe import Recipe, RecipeError

        path: Path | None = None
        if has_path:
            path = Path(str(raw_path)).expanduser().resolve(strict=True)
            recipe = load_strict_recipe(path)
            summary = recipe_summary(recipe, path)
        else:
            # Revision-only: require coffee kind + core strict validation
            # before any durable workflow create, connect, or BLE write.
            rid = str(rev_param).strip()
            revision = self.store.get_recipe_revision(rid)
            if revision is None:
                raise BridgeError(
                    f"unknown recipe_revision_id {rid!r}",
                    category="invalid_request",
                )
            recipe_kind = revision.get("recipe_kind")
            if recipe_kind is None:
                content_hint = revision.get("content") or {}
                if isinstance(content_hint, Mapping):
                    recipe_kind = content_hint.get("kind")
            if not self._recipe_kind_matches_load(recipe_kind, "coffee"):
                raise BridgeError(
                    f"recipe_revision_id {rid!r} kind {recipe_kind!r} "
                    "does not match load kind 'coffee'",
                    category="invalid_request",
                )
            try:
                content = revision.get("content")
                if not isinstance(content, Mapping):
                    raise RecipeError("revision content must be a mapping")
                recipe = Recipe.from_dict(dict(content))
                strict_validate(recipe)
            except (RecipeError, SafetyError, TypeError, ValueError, KeyError) as exc:
                raise BridgeError(
                    f"recipe_revision_id {rid!r} failed coffee validation: {exc}",
                    category="validation_error",
                ) from exc
            # Pathless summary: never invent a local recipe path.
            summary = {
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
                "recipe_sha256": revision.get("content_sha256"),
            }
        snapshot = self._snapshot_coffee_recipe(recipe, path)
        source = str(params.get("source") or "bridge")
        revision_id = self._resolve_recipe_revision_id(
            params,
            kind="coffee",
            snapshot=snapshot,
            name=recipe.name,
            path_provided=has_path,
        )

        reserved = self._reserve_request(
            "coffee.load", params, workflow_id=None
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})

        wf = self._create_durable_workflow(
            kind="coffee",
            snapshot=snapshot,
            state="loading",
            source=source,
            owner="bridge",
            recipe_revision_id=revision_id,
            metadata={
                "recipe_path": str(path) if path else None,
                "recipe_name": path.name if path else recipe.name,
            },
        )
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id

        newly_connected = False
        ble_write_attempted = False
        try:
            newly_connected = await self._ensure_connected(params, scope="workflow")
            firmware = self._require_idle_write_preflight()
            ble_write_attempted = True
            event = await self.client.load_recipe(recipe)
            if event.state_name != "armed":
                raise BridgeError(f"machine did not arm; state={event.state_name}")
        except BaseException as exc:
            if ble_write_attempted:
                # Machine load write may have taken effect: keep pending, retain
                # workflow + connection, never reissue on this request_id.
                self.phase = "load_unconfirmed"
                self.activity = self.activity or "coffee"
                self.last_error = f"coffee load outcome is unconfirmed: {exc}"
                self._set_workflow_state(
                    "load_unconfirmed",
                    workflow_id=workflow_id,
                    recovery={
                        "reason": "load_unconfirmed",
                        "request_id": request_id,
                        "error": str(exc),
                    },
                    event_type="load_unconfirmed",
                    event_payload={"error": str(exc)},
                )
                self._fail_request(request_id, str(exc), keep_pending=True)
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "load_unconfirmed",
                    "request_id": request_id,
                    "workflow_id": workflow_id,
                }
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BridgeError(
                    f"{self.last_error}; do not retry load with the same "
                    "request_id; inspect status/events then cancel or recover"
                ) from exc
            # Pre-BLE validation/connect/preflight: safe failed terminal only
            # when the durable load_failed commit succeeds. If that persist also
            # fails, retain ownership -- durable active row is still present.
            try:
                self.store.commit_workflow_terminal(
                    workflow_id,
                    state="load_failed",
                    event_type="load_failed",
                    event_payload={"error": str(exc)},
                )
            except StorageError as persist_exc:
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "load_failed_persist_failed",
                    "workflow_id": workflow_id,
                    "error": str(exc),
                    "persist_error": str(persist_exc),
                }
                self.phase = "recovery_required"
                self.activity = self.activity or "coffee"
                self.last_error = (
                    f"coffee load failed before BLE write and load_failed "
                    f"persist also failed: {persist_exc}; recovery_required"
                )
                self._fail_request(request_id, self.last_error, keep_pending=True)
                raise BridgeError(self.last_error) from persist_exc
            self.active_workflow_id = None
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._release_auto_connect_on_preflight_failure(newly_connected)
            raise
        state = {
            "address": self.address,
            "machine": self.machine_name,
            "recipe_path": str(path) if path else None,
            "recipe_sha256": (
                _sha256(path) if path is not None else summary.get("recipe_sha256")
            ),
            "loaded_at": time.time(),
            "status": "armed",
            "firmware": firmware,
            "owner": "bridge",
            "serving_kind": summary["kind"],
            "machine_program": summary["machine_program"],
            "manual_preload_ice_g": summary["manual_preload_ice_g"],
            "workflow_id": workflow_id,
            "snapshot_sha256": wf.get("snapshot_sha256"),
        }
        _atomic_json(self.coffee_state_file, state, private=True)
        self.activity = "coffee"
        self.phase = "loaded"
        self._loaded_needs_reconcile = False
        self._recovery_required = False
        self._recovery_detail = None
        self.last_error = None
        if self.connection_scope != "explicit":
            self.connection_scope = "workflow"
        self.targets = {
            "recipe": path.name if path else recipe.name,
            "target_dispensed_water_ml": recipe.total_machine_water_ml,
            "machine_program": summary["machine_program"],
            "machine_dispenses_ice": summary["machine_dispenses_ice"],
            "manual_preload_ice_g": summary["manual_preload_ice_g"],
        }
        self._saw_active = False
        self._set_workflow_state(
            "loaded",
            workflow_id=workflow_id,
            machine_phase="loaded",
            metadata={
                "recipe_path": str(path) if path else None,
                "recipe_name": path.name if path else recipe.name,
                "machine_address": self.address,
                "targets": dict(self.targets),
            },
            event_type="loaded",
            event_payload={"status": "armed", "firmware": firmware},
            critical=True,
        )
        result = {
            "status": "armed",
            "state": "loaded",
            "kind": "coffee",
            "workflow_id": workflow_id,
            "recipe_revision_id": revision_id,
            "snapshot_sha256": wf.get("snapshot_sha256"),
            "source": source,
            "recipe": path.name if path else recipe.name,
            "firmware": firmware,
            **summary,
        }
        self._complete_request(request_id, result)
        return result

    async def _coffee_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        # Exact completed duplicates after terminal/phase change return cache
        # before active-workflow or loaded-phase gates.
        cached = self._idempotency_preflight(
            "coffee.start", params, workflow_id=wid_hint
        )
        if cached is not None:
            return cached
        workflow_id, _emergency = self._require_active_workflow(params)
        self._require_hot_water(params.get("confirmation"), READY_SENTINEL)
        if self.activity != "coffee":
            raise BridgeError("bridge has no loaded coffee recipe")
        if self.phase in {"created", "loading"}:
            raise BridgeError(
                "recovery_required: coffee workflow is created/loading "
                "(unconfirmed); do not start"
            )
        if self.phase != "loaded":
            raise BridgeError("bridge has no loaded coffee recipe")
        if self._recovery_required and not self._loaded_needs_reconcile:
            raise BridgeError(
                "recovery_required: coffee start blocked until recovery clears"
            )
        reserved = self._reserve_request(
            "coffee.start", params, workflow_id=workflow_id
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        if self.coffee_state_file.exists():
            state = _read_json(self.coffee_state_file)
        else:
            # Daemon reconstruction: durable workflow is authoritative for
            # recipe identity, but machine armed state must be re-confirmed.
            wf = self.store.get_workflow(workflow_id) or {}
            meta = dict(wf.get("metadata") or {})
            state = {
                "recipe_path": meta.get("recipe_path"),
                "recipe_sha256": (wf.get("snapshot") or {}).get("_source_sha256"),
                "machine_program": "coffee-pour-over",
                "manual_preload_ice_g": 0,
                "workflow_id": workflow_id,
                "status": "armed",
            }
            self._loaded_needs_reconcile = True
        path = Path(str(state.get("recipe_path") or ""))
        if state.get("recipe_path"):
            if not path.is_file() or (
                state.get("recipe_sha256")
                and _sha256(path) != state.get("recipe_sha256")
            ):
                self._fail_request(
                    request_id, "recipe changed or disappeared", keep_pending=False
                )
                raise BridgeError("recipe changed or disappeared since it was loaded")
        if not self.connected:
            await self._ensure_connected(params, scope="workflow")
        if self._loaded_needs_reconcile:
            await self._reconcile_loaded_machine_state(
                kind="coffee", request_id=request_id
            )
        self._reset_liquid_telemetry()
        state.update(status="start_pending", start_requested_at=time.time())
        _atomic_json(self.coffee_state_file, state, private=True)
        self.phase = "starting"
        self._set_workflow_state(
            "starting",
            workflow_id=workflow_id,
            event_type="starting",
            event_payload={"request_id": request_id},
            critical=True,
        )
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
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={
                    "reason": "start_unconfirmed",
                    "request_id": request_id,
                    "error": str(exc),
                },
                event_type="start_unconfirmed",
                event_payload={"error": str(exc)},
            )
            # Keep pending so a retry never reissues start.
            self._fail_request(request_id, str(exc), keep_pending=True)
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "start_unconfirmed",
                "request_id": request_id,
                "workflow_id": workflow_id,
            }
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BridgeError(
                f"{self.last_error}; inspect bridge events/status, then cancel or use "
                "the physical control; do not retry start"
            ) from exc
        # Terminal may arrive during start (mirrors terminal-during-control).
        # Also cover durable-commit failure: activity may still be coffee while
        # phase is recovery_required -- never resurrect phase=running.
        terminal_during_start = (
            self.activity != "coffee"
            or self.active_workflow_id != workflow_id
            or self.phase in {"idle", "disconnected", "recovery_required"}
            or self._recovery_required
            or bool((self.last_operation or {}).get("persistence_failed"))
            or (
                event.state is not None
                and event.state in TERMINAL_STATE_BYTES
            )
        )
        if terminal_during_start:
            result = {
                "status": self.phase,
                "activity": self.activity,
                "workflow_id": workflow_id,
                "state": (
                    (self.last_operation or {}).get("result")
                    if self.last_operation
                    else (event.state_name if event is not None else self.phase)
                ),
                "terminal_during_start": True,
                "machine_program": state.get("machine_program", "coffee-pour-over"),
                "machine_dispenses_ice": False,
                "manual_preload_ice_g": int(state.get("manual_preload_ice_g", 0) or 0),
            }
            if self.phase == "recovery_required" or self._recovery_required:
                result["recovery_required"] = True
                # Uncertain durable terminal: keep pending so retries never
                # reissue start or claim a false completed running result.
                self._fail_request(
                    request_id,
                    self.last_error or "terminal during start with recovery_required",
                    keep_pending=True,
                )
            else:
                self._complete_request(request_id, result)
            return result
        self.phase = "running"
        self.last_error = None
        self._saw_active = event.state in ACTIVE_STATE_BYTES or self._saw_active
        state.update(status="running", started_at=time.time(), last_state=event.state_name)
        _atomic_json(self.coffee_state_file, state, private=True)
        self._set_workflow_state(
            "running",
            workflow_id=workflow_id,
            event_type="started",
            event_payload={"state": event.state_name},
            critical=True,
        )
        result = {
            "status": "running",
            "state": event.state_name,
            "workflow_id": workflow_id,
            "machine_program": state.get("machine_program", "coffee-pour-over"),
            "machine_dispenses_ice": False,
            "manual_preload_ice_g": int(state.get("manual_preload_ice_g", 0) or 0),
        }
        self._complete_request(request_id, result)
        return result

    async def _tea_load(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        cached = self._idempotency_preflight("tea.load", params, workflow_id=None)
        if cached is not None:
            return cached
        self._ensure_no_loaded_record()
        if self.active_workflow_id is not None and self.activity is not None:
            raise BridgeError(
                f"active workflow {self.active_workflow_id} still owns the bridge"
            )
        raw_path = params.get("recipe")
        rev_param = params.get("recipe_revision_id")
        has_path = raw_path is not None and str(raw_path).strip() != ""
        has_rev = rev_param is not None and str(rev_param).strip() != ""
        if not has_path and not has_rev:
            raise BridgeError(
                "tea.load requires a local recipe path or recipe_revision_id",
                category="invalid_request",
            )
        from .tea import TeaRecipe, TeaRecipeError

        path: Path | None = None
        revision_content_sha: str | None = None
        if has_path:
            path = Path(str(raw_path)).expanduser().resolve(strict=True)
            recipe = TeaRecipe.from_yaml(path)
        else:
            # Revision-only parity with coffee.load: resolve from StateStore,
            # require tea kind, validate TeaRecipe before any BLE action.
            # Never invent a temporary file path.
            rid = str(rev_param).strip()
            revision = self.store.get_recipe_revision(rid)
            if revision is None:
                raise BridgeError(
                    f"unknown recipe_revision_id {rid!r}",
                    category="invalid_request",
                )
            recipe_kind = revision.get("recipe_kind")
            if recipe_kind is None:
                content_hint = revision.get("content") or {}
                if isinstance(content_hint, Mapping):
                    recipe_kind = content_hint.get("kind")
            if not self._recipe_kind_matches_load(recipe_kind, "tea"):
                raise BridgeError(
                    f"recipe_revision_id {rid!r} kind {recipe_kind!r} "
                    "does not match load kind 'tea'",
                    category="invalid_request",
                )
            try:
                content = revision.get("content")
                if not isinstance(content, Mapping):
                    raise TeaRecipeError("revision content must be a mapping")
                recipe = TeaRecipe.from_dict(dict(content))
            except (TeaRecipeError, TypeError, ValueError, KeyError) as exc:
                raise BridgeError(
                    f"recipe_revision_id {rid!r} failed tea validation: {exc}",
                    category="validation_error",
                ) from exc
            revision_content_sha = revision.get("content_sha256")
        snapshot = self._snapshot_tea_recipe(recipe, path)
        source = str(params.get("source") or "bridge")

        revision_id = self._resolve_recipe_revision_id(
            params,
            kind="tea",
            snapshot=snapshot,
            name=recipe.name,
            path_provided=has_path,
        )
        reserved = self._reserve_request("tea.load", params, workflow_id=None)
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        recipe_name = path.name if path is not None else recipe.name
        wf = self._create_durable_workflow(
            kind="tea",
            snapshot=snapshot,
            state="loading",
            source=source,
            owner="bridge",
            recipe_revision_id=revision_id,
            metadata={
                "recipe_path": str(path) if path is not None else None,
                "recipe_name": recipe_name,
            },
        )
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id

        newly_connected = False
        ble_write_attempted = False
        try:
            newly_connected = await self._ensure_connected(params, scope="workflow")
            firmware = self._require_idle_write_preflight()
            ble_write_attempted = True
            event = await self.client.load_tea_recipe(recipe)
        except BaseException as exc:
            if ble_write_attempted:
                self.phase = "load_unconfirmed"
                self.activity = self.activity or "tea"
                self.last_error = f"tea load outcome is unconfirmed: {exc}"
                self._set_workflow_state(
                    "load_unconfirmed",
                    workflow_id=workflow_id,
                    recovery={
                        "reason": "load_unconfirmed",
                        "request_id": request_id,
                        "error": str(exc),
                    },
                    event_type="load_unconfirmed",
                    event_payload={"error": str(exc)},
                )
                self._fail_request(request_id, str(exc), keep_pending=True)
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "load_unconfirmed",
                    "request_id": request_id,
                    "workflow_id": workflow_id,
                }
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BridgeError(
                    f"{self.last_error}; do not retry load with the same "
                    "request_id; inspect status/events then cancel or recover"
                ) from exc
            try:
                self.store.commit_workflow_terminal(
                    workflow_id,
                    state="load_failed",
                    event_type="load_failed",
                    event_payload={"error": str(exc)},
                )
            except StorageError as persist_exc:
                self._recovery_required = True
                self._recovery_detail = {
                    "reason": "load_failed_persist_failed",
                    "workflow_id": workflow_id,
                    "error": str(exc),
                    "persist_error": str(persist_exc),
                }
                self.phase = "recovery_required"
                self.activity = self.activity or "tea"
                self.last_error = (
                    f"tea load failed before BLE write and load_failed "
                    f"persist also failed: {persist_exc}; recovery_required"
                )
                self._fail_request(request_id, self.last_error, keep_pending=True)
                raise BridgeError(self.last_error) from persist_exc
            self.active_workflow_id = None
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._release_auto_connect_on_preflight_failure(newly_connected)
            raise
        state = {
            "address": self.address,
            "machine": self.machine_name,
            "recipe_path": str(path) if path is not None else None,
            "recipe_sha256": (
                _sha256(path) if path is not None else revision_content_sha
            ),
            "loaded_at": time.time(),
            "status": "tea_loaded",
            "firmware": firmware,
            "owner": "bridge",
            "workflow_id": workflow_id,
            "snapshot_sha256": wf.get("snapshot_sha256"),
        }
        _atomic_json(self.tea_state_file, state, private=True)
        self.activity = "tea"
        self.phase = "loaded"
        self._loaded_needs_reconcile = False
        self._recovery_required = False
        self._recovery_detail = None
        self.last_error = None
        if self.connection_scope != "explicit":
            self.connection_scope = "workflow"
        self.targets = {
            "recipe": recipe_name,
            "target_dispensed_water_ml": sum(pour.ml for pour in recipe.pours),
            "leaf_g": recipe.leaf_g,
            "steeps": len(recipe.pours),
        }
        self._saw_active = False
        self._set_workflow_state(
            "loaded",
            workflow_id=workflow_id,
            machine_phase="loaded",
            metadata={
                "recipe_path": str(path) if path is not None else None,
                "recipe_name": recipe_name,
                "machine_address": self.address,
                "targets": dict(self.targets),
            },
            event_type="loaded",
            event_payload={"status": "tea_loaded", "firmware": firmware},
            critical=True,
        )
        result = {
            "status": "tea_loaded",
            "state": "loaded",
            "kind": "tea",
            "workflow_id": workflow_id,
            "recipe_revision_id": revision_id,
            "snapshot_sha256": wf.get("snapshot_sha256"),
            "source": source,
            "recipe": recipe_name,
            "firmware": firmware,
            "ack": event.command_code,
            "summary": recipe.summary(),
        }
        self._complete_request(request_id, result)
        return result

    async def _tea_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        cached = self._idempotency_preflight(
            "tea.start", params, workflow_id=wid_hint
        )
        if cached is not None:
            return cached
        workflow_id, _emergency = self._require_active_workflow(params)
        self._require_hot_water(params.get("confirmation"), TEA_READY_SENTINEL)
        if self.activity != "tea":
            raise BridgeError("bridge has no loaded tea recipe")
        if self.phase in {"created", "loading"}:
            raise BridgeError(
                "recovery_required: tea workflow is created/loading "
                "(unconfirmed); do not start"
            )
        if self.phase != "loaded":
            raise BridgeError("bridge has no loaded tea recipe")
        if self._recovery_required and not self._loaded_needs_reconcile:
            raise BridgeError(
                "recovery_required: tea start blocked until recovery clears"
            )
        reserved = self._reserve_request(
            "tea.start", params, workflow_id=workflow_id
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        if self.tea_state_file.exists():
            state = _read_json(self.tea_state_file)
        else:
            wf = self.store.get_workflow(workflow_id) or {}
            meta = dict(wf.get("metadata") or {})
            state = {
                "recipe_path": meta.get("recipe_path"),
                "recipe_sha256": (wf.get("snapshot") or {}).get("_source_sha256"),
                "workflow_id": workflow_id,
                "status": "tea_loaded",
            }
            self._loaded_needs_reconcile = True
        # Pathless revision-only loads store null recipe_path; only re-check a
        # local file when Skill/MCP provided one at load time.
        if state.get("recipe_path"):
            path = Path(str(state.get("recipe_path") or ""))
            if not path.is_file() or (
                state.get("recipe_sha256")
                and _sha256(path) != state.get("recipe_sha256")
            ):
                self._fail_request(
                    request_id,
                    "tea recipe changed or disappeared",
                    keep_pending=False,
                )
                raise BridgeError(
                    "tea recipe changed or disappeared since it was loaded"
                )
        if not self.connected:
            await self._ensure_connected(params, scope="workflow")
        if self._loaded_needs_reconcile:
            await self._reconcile_loaded_machine_state(
                kind="tea", request_id=request_id
            )
        self._reset_liquid_telemetry()
        self.phase = "starting"
        self._set_workflow_state(
            "starting",
            workflow_id=workflow_id,
            event_type="starting",
            event_payload={"request_id": request_id},
            critical=True,
        )
        try:
            event = await self.client.start_tea()
        except BaseException as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"tea start outcome is unconfirmed: {exc}"
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={"reason": "start_unconfirmed", "error": str(exc)},
                event_type="start_unconfirmed",
                event_payload={"error": str(exc)},
            )
            self._fail_request(request_id, str(exc), keep_pending=True)
            self._recovery_required = True
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BridgeError(
                f"{self.last_error}; do not retry start"
            ) from exc
        terminal_during_start = (
            self.activity != "tea"
            or self.active_workflow_id != workflow_id
            or self.phase in {"idle", "disconnected", "recovery_required"}
            or self._recovery_required
            or bool((self.last_operation or {}).get("persistence_failed"))
            or (
                event.state is not None
                and event.state in TERMINAL_STATE_BYTES
            )
        )
        if terminal_during_start:
            result = {
                "status": self.phase,
                "activity": self.activity,
                "workflow_id": workflow_id,
                "state": (
                    (self.last_operation or {}).get("result")
                    if self.last_operation
                    else (event.state_name if event is not None else self.phase)
                ),
                "ack": event.command_code,
                "terminal_during_start": True,
            }
            if self.phase == "recovery_required" or self._recovery_required:
                result["recovery_required"] = True
                self._fail_request(
                    request_id,
                    self.last_error or "terminal during start with recovery_required",
                    keep_pending=True,
                )
            else:
                self._complete_request(request_id, result)
            return result
        self.phase = "running"
        self.last_error = None
        # Dedicated tea activity reports do not consistently carry the generic
        # coffee active-state byte. A confirmed 4512 response is the activation
        # boundary; a later terminal state may safely finish the bridge activity.
        self._saw_active = True
        state.update(status="running", started_at=time.time(), last_state=event.state_name)
        _atomic_json(self.tea_state_file, state, private=True)
        self._set_workflow_state(
            "running",
            workflow_id=workflow_id,
            event_type="started",
            event_payload={"state": event.state_name, "ack": event.command_code},
            critical=True,
        )
        result = {
            "status": "running",
            "state": event.state_name,
            "ack": event.command_code,
            "workflow_id": workflow_id,
        }
        self._complete_request(request_id, result)
        return result

    async def _scale_start(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        cached = self._idempotency_preflight(
            "scale.start", params, workflow_id=None
        )
        if cached is not None:
            return cached
        duration = float(params.get("duration_s", 30.0))
        raw_tare = params.get("tare", False)
        if not isinstance(raw_tare, bool):
            raise BridgeError("scale tare must be a boolean")
        tare = raw_tare
        if not 0.1 <= duration <= 3600:
            raise BridgeError("scale duration must be 0.1-3600 seconds")
        self._ensure_no_loaded_record()
        reserved = self._reserve_request("scale.start", params, workflow_id=None)
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        newly_connected = await self._ensure_connected(params, scope="one-shot")
        try:
            firmware = self._require_idle_write_preflight()
        except BaseException as exc:
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._release_auto_connect_on_preflight_failure(newly_connected)
            raise
        wf = self._create_durable_workflow(
            kind="scale",
            snapshot={"duration_s": duration, "tare": tare},
            state="starting",
            source=str(params.get("source") or "bridge"),
            metadata={"targets": {"duration_s": duration, "tare": tare}},
        )
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id
        self.activity = "scale"
        self.phase = "starting"
        if self.connection_scope != "explicit":
            self.connection_scope = "one-shot"
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
                self._set_workflow_state("running", workflow_id=workflow_id)

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
                # External stop/cancel owns the single terminal+idempotency
                # commit. Do not finalize here when _stop_scale is driving.
                if self.activity == "scale" and not self._scale_stop_in_progress:
                    self._finish_activity("stopped", release_reason="scale_stopped")
                raise
            except Exception as exc:
                if self.activity == "scale" and not self._scale_stop_in_progress:
                    self._finish_activity("failed", release_reason="scale_failed")
                self.last_error = f"scale session failed: {exc}"
            else:
                if self.activity == "scale" and not self._scale_stop_in_progress:
                    self._finish_activity("complete", release_reason="scale_complete")
            finally:
                if asyncio.current_task() is self._scale_task:
                    self._scale_task = None

        self._scale_task = asyncio.create_task(run())
        await asyncio.sleep(0)
        result = {
            "status": self.phase,
            "firmware": firmware,
            "workflow_id": workflow_id,
            "kind": "scale",
            "snapshot_sha256": wf.get("snapshot_sha256"),
            **target_snapshot,
        }
        self._complete_request(request_id, result)
        return result

    async def _scale_tare(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = {} if params is None else dict(params)
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        cached = self._idempotency_preflight(
            "scale.tare", params, workflow_id=wid_hint
        )
        if cached is not None:
            return cached
        workflow_id, _ = self._require_active_workflow(params)
        if self.activity != "scale" or self.phase != "running":
            raise BridgeError("scale tare requires a running scale session")
        reserved = self._reserve_request(
            "scale.tare", params, workflow_id=workflow_id
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        await self.client.tare_scale()
        result = {
            "status": "running",
            "activity": "scale",
            "workflow_id": workflow_id,
            "command_write_verified": True,
            "report_observed": False,
        }
        self._complete_request(request_id, result)
        return result

    async def _stop_scale(
        self,
        reason: str,
        *,
        request_id: str | None = None,
        emergency: bool = False,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        # Exactly one terminal commit for stop/cancel: task cancellation must
        # not finalize before this path can supply request_id.
        self._scale_stop_in_progress = True
        try:
            task = self._scale_task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            result = {
                "status": "stopped",
                "activity": "scale",
                "workflow_id": workflow_id or self.active_workflow_id,
            }
            if emergency:
                result["emergency"] = True
            if self.activity == "scale":
                self._finish_activity(
                    reason,
                    release_reason="scale_stopped",
                    emergency=emergency,
                    request_id=request_id,
                    idempotency_result=result if request_id else None,
                )
                if request_id and self.phase == "recovery_required":
                    raise BridgeError(
                        self.last_error
                        or "scale stop confirmed but durable terminal commit failed"
                    )
            elif request_id:
                # Natural complete/fail already terminalized; complete request only.
                self._complete_request(request_id, result)
            return result
        finally:
            self._scale_stop_in_progress = False

    async def _save_presets(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        cached = self._idempotency_preflight(
            "presets.save", params, workflow_id=None
        )
        if cached is not None:
            return cached
        raw_recipes = params.get("recipes")
        if not isinstance(raw_recipes, list) or len(raw_recipes) != 3:
            raise BridgeError(
                "presets.save requires exactly three recipe paths (A/B/C)"
            )
        self._ensure_no_loaded_record()
        if self.active_workflow_id is not None and self.activity is not None:
            raise BridgeError(
                f"active workflow {self.active_workflow_id} still owns the bridge"
            )
        from xbloom_safety import load_strict_recipe, validate_slot_compatible

        # Validate all three recipes before durable workflow or any BLE write.
        paths = [
            Path(str(item)).expanduser().resolve(strict=True) for item in raw_recipes
        ]
        recipes = [load_strict_recipe(path) for path in paths]
        for recipe in recipes:
            validate_slot_compatible(recipe)
        scale = params.get("scale", True)
        if not isinstance(scale, (bool, list)):
            raise BridgeError("presets scale must be a boolean or three booleans")
        if isinstance(scale, list) and len(scale) != 3:
            raise BridgeError("presets scale list must have exactly three values")
        if isinstance(scale, list) and not all(
            isinstance(value, bool) for value in scale
        ):
            raise BridgeError("presets scale list values must be booleans")
        reserved = self._reserve_request(
            "presets.save", params, workflow_id=None
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        # Pre-write zone: connect / preflight / durable create. Retryable on failure.
        firmware = ""
        wf: dict[str, Any]
        try:
            await self._ensure_connected(params, scope="one-shot")
            firmware = self._require_idle_write_preflight()
            slot_snapshots = [
                self._snapshot_coffee_recipe(recipe, path)
                for path, recipe in zip(paths, recipes)
            ]
            snapshot = {
                "kind": "presets",
                "slots": slot_snapshots,
                "slot_sha256": [content_sha256(item) for item in slot_snapshots],
                "scale": scale if isinstance(scale, bool) else list(scale),
                "names": [recipe.name for recipe in recipes],
            }
            wf = self._create_durable_workflow(
                kind="presets",
                snapshot=snapshot,
                state="writing",
                source=str(params.get("source") or "bridge"),
                metadata={
                    "slots": [recipe.name for recipe in recipes],
                    "machine_address": self.address,
                },
            )
        except BaseException as exc:
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._prompt_release_auto_owned("presets_save_preflight_failed")
            raise
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id
        self.activity = "presets"
        self.phase = "writing"
        self.targets = {"slots": [recipe.name for recipe in recipes]}
        if self.connection_scope != "explicit":
            self.connection_scope = "one-shot"
        try:
            await self.client.save_slots(recipes, scale=scale)
        except Exception as exc:
            # Client contract: exception means partial/unconfirmed unless proven
            # otherwise. Keep pending idempotency, durable ownership, and BLE.
            error = f"A/B/C preset write outcome is unconfirmed: {exc}"
            self.phase = "control_unconfirmed"
            self.last_error = error
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={
                    "reason": "control_unconfirmed",
                    "request_id": request_id,
                    "error": str(exc),
                },
                event_type="control_unconfirmed",
                event_payload={"error": str(exc)},
            )
            self._fail_request(request_id, error, keep_pending=True)
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "control_unconfirmed",
                "request_id": request_id,
                "workflow_id": workflow_id,
            }
            raise BridgeError(
                f"{error}; recovery_required; do not retry the machine write"
            ) from exc
        names = [recipe.name for recipe in recipes]
        result = {
            "status": "saved",
            "firmware": firmware,
            "slots": names,
            "brew_started": False,
            "workflow_id": workflow_id,
            "kind": "presets",
            "snapshot_sha256": wf.get("snapshot_sha256"),
        }
        self.last_error = None
        self._finish_activity(
            "saved",
            release_reason="presets_save_complete",
            request_id=request_id,
            idempotency_result=result,
        )
        if self.phase == "recovery_required" or self._recovery_required:
            raise BridgeError(
                self.last_error
                or (
                    "presets save confirmed on machine but durable terminal "
                    "commit failed; recovery_required; do not retry the write"
                )
            )
        return result

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
        request_id = self._require_request_id(params)
        # Completed duplicates during cooldown must return cache before rest gate.
        cached = self._idempotency_preflight(
            "grinder.start", params, workflow_id=None
        )
        if cached is not None:
            return cached
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
        reserved = self._reserve_request("grinder.start", params, workflow_id=None)
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        newly_connected = await self._ensure_connected(params, scope="one-shot")
        try:
            firmware = self._require_idle_write_preflight()
        except BaseException as exc:
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._release_auto_connect_on_preflight_failure(newly_connected)
            raise
        wf = self._create_durable_workflow(
            kind="grinder",
            snapshot={"size": size, "rpm": rpm, "seconds": seconds},
            state="starting",
            source=str(params.get("source") or "bridge"),
            metadata={
                "targets": {"size": size, "rpm": rpm, "runtime_s": seconds},
                "machine_address": self.address,
            },
        )
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id
        self._write_grinder_running_record(seconds)
        self.activity = "grinder"
        self.phase = "starting"
        if self.connection_scope != "explicit":
            self.connection_scope = "one-shot"
        self.targets = {"size": size, "rpm": rpm, "runtime_s": seconds}
        try:
            await self.client.start_grinder_session(size, rpm)
        except Exception as exc:
            self._fail_request(request_id, str(exc), keep_pending=True)
            await self._abort_grinder_after_control_error("start", exc)
        self.phase = "running"
        self.last_error = None
        self._grinder_remaining = seconds
        self._start_grinder_timer()
        self._set_workflow_state(
            "running",
            workflow_id=workflow_id,
            event_type="started",
            event_payload={"size": size, "rpm": rpm, "seconds": seconds},
        )
        result = {
            "status": "running",
            "firmware": firmware,
            "workflow_id": workflow_id,
            "kind": "grinder",
            "snapshot_sha256": wf.get("snapshot_sha256"),
            **self.targets,
        }
        self._complete_request(request_id, result)
        return result

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
        self._finish_activity(
            f"{operation}_failed_stopped",
            release_reason="grinder_confirmed_stop",
        )
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
        request_id = self._require_request_id(params)
        cached = self._idempotency_preflight(
            "water.start", params, workflow_id=None
        )
        if cached is not None:
            return cached
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
        reserved = self._reserve_request("water.start", params, workflow_id=None)
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        newly_connected = await self._ensure_connected(params, scope="one-shot")
        try:
            firmware = self._require_idle_write_preflight()
            if source == "auto":
                source = str(self.machine_info.get("water_source") or "")
            if source not in {"tank", "tap"}:
                raise BridgeError("water source must be tank/tap or readable via auto")
        except BaseException as exc:
            self._fail_request(request_id, str(exc), keep_pending=False)
            await self._release_auto_connect_on_preflight_failure(newly_connected)
            raise
        water_feed = {"tank": 0, "tap": 1}[source]
        wf = self._create_durable_workflow(
            kind="water",
            snapshot={
                "volume_ml": volume,
                "temp_c": temp,
                "flow_ml_s": flow,
                "pattern": pattern,
                "water_source": source,
            },
            state="starting",
            source=str(params.get("source") or "bridge"),
            metadata={"machine_address": self.address},
        )
        workflow_id = str(wf["workflow_id"])
        self.active_workflow_id = workflow_id
        self.activity = "water"
        self.phase = "starting"
        if self.connection_scope != "explicit":
            self.connection_scope = "one-shot"
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
                self._fail_request(request_id, str(stop_exc), keep_pending=True)
                raise BridgeError(self.last_error) from stop_exc
            self._finish_activity(
                "start_failed_stopped", release_reason="water_confirmed_stop"
            )
            self.last_error = "water start failed; STOP/QUIT was confirmed"
            self._fail_request(request_id, self.last_error, keep_pending=False)
            raise BridgeError(self.last_error) from exc
        self.phase = "running"
        self.last_error = None
        self._start_water_timer(float(self.targets["safety_timeout_s"]))
        self._set_workflow_state(
            "running",
            workflow_id=workflow_id,
            event_type="started",
            event_payload={"volume_ml": volume, "temp_c": temp},
        )
        result = {
            "status": "running",
            "firmware": firmware,
            "workflow_id": workflow_id,
            "kind": "water",
            "snapshot_sha256": wf.get("snapshot_sha256"),
            **self.targets,
        }
        self._complete_request(request_id, result)
        return result

    async def _pause(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = {} if params is None else dict(params)
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        cached = self._idempotency_preflight("pause", params, workflow_id=wid_hint)
        if cached is not None:
            return cached
        workflow_id, _ = self._require_active_workflow(params)
        if self.activity not in {"coffee", "grinder", "water"} or self.phase != "running":
            raise BridgeError("pause requires a running coffee, grinder, or water activity")
        reserved = self._reserve_request("pause", params, workflow_id=workflow_id)
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
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
                self._fail_request(request_id, str(exc), keep_pending=True)
                await self._abort_grinder_after_control_error("pause", exc)
            self.phase = "control_unconfirmed"
            self.last_error = f"{activity} pause outcome is unconfirmed: {exc}"
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={"reason": "pause_unconfirmed", "error": str(exc)},
            )
            self._fail_request(request_id, str(exc), keep_pending=True)
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        terminal_during_control = (
            self.activity != activity
            or self.active_workflow_id != workflow_id
            or self.phase in {"idle", "disconnected", "recovery_required"}
            or self._recovery_required
            or bool((self.last_operation or {}).get("persistence_failed"))
            or (
                getattr(event, "state", None) is not None
                and event.state in TERMINAL_STATE_BYTES
            )
        )
        if terminal_during_control:
            result = {
                "status": self.phase,
                "activity": self.activity,
                "workflow_id": workflow_id,
                "ack": event.command_code,
                "terminal_during_control": True,
                "state": (
                    (self.last_operation or {}).get("result")
                    if self.last_operation
                    else self.phase
                ),
            }
            if self.phase == "recovery_required" or self._recovery_required:
                result["recovery_required"] = True
                self._fail_request(
                    request_id,
                    self.last_error
                    or "terminal during pause with recovery_required",
                    keep_pending=True,
                )
            else:
                self._complete_request(request_id, result)
            return result
        self.phase = "paused"
        self._set_workflow_state(
            "paused",
            workflow_id=workflow_id,
            event_type="paused",
            event_payload={"activity": activity},
            critical=True,
        )
        result = {
            "status": "paused",
            "activity": activity,
            "workflow_id": workflow_id,
            "ack": event.command_code,
        }
        self._complete_request(request_id, result)
        return result

    async def _resume(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = {} if params is None else dict(params)
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        cached = self._idempotency_preflight("resume", params, workflow_id=wid_hint)
        if cached is not None:
            return cached
        workflow_id, _ = self._require_active_workflow(params)
        if self.activity not in {"coffee", "grinder", "water"} or self.phase != "paused":
            raise BridgeError("resume requires a paused coffee, grinder, or water activity")
        reserved = self._reserve_request("resume", params, workflow_id=workflow_id)
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        activity = self.activity
        if activity == "grinder" and self._grinder_remaining <= 0:
            self._fail_request(
                request_id, "grinder runtime exhausted", keep_pending=False
            )
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
                self._fail_request(request_id, str(exc), keep_pending=True)
                await self._abort_grinder_after_control_error("resume", exc)
            self.phase = "control_unconfirmed"
            self.last_error = f"{activity} resume outcome is unconfirmed: {exc}"
            self._set_workflow_state(
                "control_unconfirmed",
                workflow_id=workflow_id,
                recovery={"reason": "resume_unconfirmed", "error": str(exc)},
            )
            self._fail_request(request_id, str(exc), keep_pending=True)
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        terminal_during_control = (
            self.activity != activity
            or self.active_workflow_id != workflow_id
            or self.phase in {"idle", "disconnected", "recovery_required"}
            or self._recovery_required
            or bool((self.last_operation or {}).get("persistence_failed"))
            or (
                getattr(event, "state", None) is not None
                and event.state in TERMINAL_STATE_BYTES
            )
        )
        if terminal_during_control:
            result = {
                "status": self.phase,
                "activity": self.activity,
                "workflow_id": workflow_id,
                "ack": event.command_code,
                "terminal_during_control": True,
                "state": (
                    (self.last_operation or {}).get("result")
                    if self.last_operation
                    else self.phase
                ),
            }
            if self.phase == "recovery_required" or self._recovery_required:
                result["recovery_required"] = True
                self._fail_request(
                    request_id,
                    self.last_error
                    or "terminal during resume with recovery_required",
                    keep_pending=True,
                )
            else:
                self._complete_request(request_id, result)
            return result
        self.phase = "running"
        self._set_workflow_state(
            "running",
            workflow_id=workflow_id,
            event_type="resumed",
            event_payload={"activity": activity},
            critical=True,
        )
        result = {
            "status": "running",
            "activity": activity,
            "workflow_id": workflow_id,
            "ack": event.command_code,
        }
        self._complete_request(request_id, result)
        return result

    async def _stop_grinder(
        self,
        reason: str,
        *,
        request_id: str | None = None,
        emergency: bool = False,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        self.phase = "stopping"
        self._cancel_grinder_timer()
        try:
            event = await self.client.stop_grinder_session()
        except Exception as exc:
            self.phase = "stop_unconfirmed"
            self.last_error = f"grinder STOP/QUIT is unconfirmed: {exc}"
            if request_id:
                self._fail_request(request_id, str(exc), keep_pending=True)
            raise BridgeError(self.last_error) from exc
        self._write_grinder_stopped_record()
        result = {
            "status": "stopped",
            "activity": "grinder",
            "ack": event.command_code,
            "workflow_id": workflow_id or self.active_workflow_id,
        }
        if emergency:
            result["emergency"] = True
        self._finish_activity(
            reason,
            release_reason="grinder_confirmed_stop",
            emergency=emergency,
            request_id=request_id,
            idempotency_result=result if request_id else None,
        )
        if request_id and self.phase == "recovery_required":
            raise BridgeError(
                self.last_error
                or "grinder stop confirmed but durable terminal commit failed"
            )
        return result

    async def _stop_water(
        self,
        reason: str,
        *,
        request_id: str | None = None,
        emergency: bool = False,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
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
            if request_id:
                self._fail_request(request_id, str(exc), keep_pending=True)
            raise BridgeError(self.last_error) from exc
        result = {
            "status": "stopped",
            "activity": "water",
            "ack": event.command_code,
            "workflow_id": workflow_id or self.active_workflow_id,
        }
        if emergency:
            result["emergency"] = True
        self._finish_activity(
            reason,
            release_reason="water_confirmed_stop",
            emergency=emergency,
            request_id=request_id,
            idempotency_result=result if request_id else None,
            target_volume_ml=target,
            metered_volume_ml=metered,
        )
        if request_id and self.phase == "recovery_required":
            raise BridgeError(
                self.last_error
                or "water stop confirmed but durable terminal commit failed"
            )
        return result

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
        await self._connect_unlocked({"address": str(address)}, scope="workflow")
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
        self._finish_activity(
            "recovery_cancel_sent", release_reason="recovery_cancel"
        )
        self.last_error = None
        return {
            "status": "recovery_cancel_sent",
            "activity": kind,
            "record_cleared": True,
        }

    async def _stop(
        self,
        params: Mapping[str, Any] | None = None,
        *,
        rpc_method: str = "cancel",
    ) -> dict[str, Any]:
        params = {} if params is None else dict(params)
        request_id = self._require_request_id(params)
        emergency = bool(params.get("emergency"))
        # Preserve the actual RPC method identity for stop vs cancel. Emergency
        # is a param; it must not silently rename the idempotency method.
        method_name = rpc_method if rpc_method in {"stop", "cancel"} else "cancel"
        wid_hint = self._workflow_id_hint(params)
        # Exact completed duplicates after terminal return cache before activity gates.
        cached = self._idempotency_preflight(
            method_name, params, workflow_id=wid_hint
        )
        if cached is not None:
            return cached
        # Legacy JSON recovery without an in-memory activity: allow without
        # matching workflow_id (explicit recovery path).
        if self.activity is None:
            if self.coffee_state_file.exists() or self.tea_state_file.exists():
                reserved = self._reserve_request(
                    method_name, params, workflow_id=self.active_workflow_id
                )
                if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
                    return dict(reserved.get("result") or {})
                recovered = await self._recover_loaded_record()
                if recovered is not None:
                    recovered = dict(recovered)
                    recovered["workflow_id"] = self.active_workflow_id
                    if emergency:
                        recovered["emergency"] = True
                    self._complete_request(request_id, recovered)
                    return recovered
            raise BridgeError("there is no bridge-owned activity to stop")

        workflow_id, used_emergency = self._require_active_workflow(
            params, emergency=emergency
        )
        reserved = self._reserve_request(
            method_name,
            params,
            workflow_id=workflow_id or self.active_workflow_id,
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})

        if self.activity == "coffee":
            self.phase = "stopping"
            if not self.connected:
                await self._ensure_connected(params, scope="workflow")
            try:
                await self.client.cancel_brew()
            except Exception as exc:
                self.phase = "stop_unconfirmed"
                self.last_error = f"coffee cancel is unconfirmed: {exc}"
                self._set_workflow_state(
                    "stop_unconfirmed",
                    recovery={"reason": "cancel_unconfirmed", "error": str(exc)},
                )
                self._fail_request(request_id, str(exc), keep_pending=True)
                raise BridgeError(self.last_error) from exc
            _unlink(self.coffee_state_file)
            result = {
                "status": "cancel_sent",
                "activity": "coffee",
                "workflow_id": workflow_id or None,
            }
            if used_emergency:
                result["emergency"] = True
            self._finish_activity(
                "cancel_sent",
                release_reason="cancel",
                emergency=used_emergency,
                request_id=request_id,
                idempotency_result=result,
            )
            if self.phase == "recovery_required":
                raise BridgeError(
                    self.last_error
                    or "cancel confirmed but durable terminal commit failed"
                )
            return result
        if self.activity == "tea":
            self.phase = "stopping"
            if not self.connected:
                await self._ensure_connected(params, scope="workflow")
            try:
                await self.client.unload_tea_recipe()
            except Exception as exc:
                self.phase = "stop_unconfirmed"
                self.last_error = f"tea cancel/exit is unconfirmed: {exc}"
                self._set_workflow_state(
                    "stop_unconfirmed",
                    recovery={"reason": "cancel_unconfirmed", "error": str(exc)},
                )
                self._fail_request(request_id, str(exc), keep_pending=True)
                raise BridgeError(self.last_error) from exc
            _unlink(self.tea_state_file)
            result = {
                "status": "cancel_sent",
                "activity": "tea",
                "workflow_id": workflow_id or None,
            }
            if used_emergency:
                result["emergency"] = True
            self._finish_activity(
                "cancel_sent",
                release_reason="cancel",
                emergency=used_emergency,
                request_id=request_id,
                idempotency_result=result,
            )
            if self.phase == "recovery_required":
                raise BridgeError(
                    self.last_error
                    or "cancel confirmed but durable terminal commit failed"
                )
            return result
        if self.activity == "scale":
            result = await self._stop_scale(
                "stopped",
                request_id=request_id,
                emergency=used_emergency,
                workflow_id=workflow_id or self.active_workflow_id,
            )
            return result
        if self.activity == "grinder":
            result = await self._stop_grinder(
                "stopped",
                request_id=request_id,
                emergency=used_emergency,
                workflow_id=workflow_id or None,
            )
            return result
        if self.activity == "water":
            result = await self._stop_water(
                "stopped",
                request_id=request_id,
                emergency=used_emergency,
                workflow_id=workflow_id or None,
            )
            return result
        if self.activity in {"settings", "advanced", "presets"}:
            # One-shot writes have no machine cancel opcode. stop/cancel may
            # explicitly abandon bridge ownership, but must not claim a machine
            # cancel or rollback. Original write request_id stays pending forever
            # (never reissued); machine effect remains unknown.
            activity = self.activity
            result = {
                "status": "recovery_released",
                "result": "ownership_released_unconfirmed",
                "activity": activity,
                "workflow_id": workflow_id or self.active_workflow_id,
                "machine_cancel": False,
                "machine_effect_unknown": True,
                "note": (
                    "no machine cancel opcode for settings/advanced/presets; "
                    "bridge ownership released without confirming machine state; "
                    "original write request_id remains pending and must not be reissued"
                ),
            }
            if used_emergency:
                result["emergency"] = True
            self._finish_activity(
                "ownership_released_unconfirmed",
                release_reason="recovery_released",
                emergency=used_emergency,
                request_id=request_id,
                idempotency_result=result,
                machine_cancel=False,
                machine_effect_unknown=True,
            )
            if self.phase == "recovery_required":
                raise BridgeError(
                    self.last_error
                    or (
                        "recovery release durable terminal commit failed; "
                        "recovery_required"
                    )
                )
            return result
        raise BridgeError(f"stop is not implemented for activity {self.activity}")

    async def _set_water_temperature(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        cached = self._idempotency_preflight(
            "water.set_temperature", params, workflow_id=wid_hint
        )
        if cached is not None:
            return cached
        workflow_id, _ = self._require_active_workflow(params)
        self._require_live_adjust(params.get("confirmation"))
        if self.activity != "water" or self.phase not in {"running", "paused"}:
            raise BridgeError("temperature adjustment requires running/paused FreeSolo water")
        reserved = self._reserve_request(
            "water.set_temperature", params, workflow_id=workflow_id
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        temp = int(params.get("temp_c", -1))
        if temp != ROOM_TEMPERATURE_C and not 40 <= temp <= 98:
            self._fail_request(request_id, "invalid temp", keep_pending=False)
            raise BridgeError("water temperature must be RT or 40-98 C")
        try:
            event = await self.client.set_water_temperature(temp)
        except Exception as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"water temperature adjustment outcome is unconfirmed: {exc}"
            self._fail_request(request_id, str(exc), keep_pending=True)
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        self.targets["temp_c"] = temp
        self.targets["temp_setting"] = "RT" if temp == ROOM_TEMPERATURE_C else f"{temp} C"
        result = {
            "status": self.phase,
            "activity": "water",
            "workflow_id": workflow_id,
            "target_temp_c": temp,
            "report": event.command_code if event is not None else None,
            "report_observed": event is not None,
            "command_write_verified": True,
            "outlet_temperature_effect_measured": False,
            # Compatibility field retained for existing bridge consumers. A
            # correct BLE write is not a physical outlet-temperature measure.
            "hardware_effect_verified": False,
        }
        self._complete_request(request_id, result)
        return result

    async def _set_water_pattern(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._require_request_id(params)
        wid_hint = self._workflow_id_hint(params)
        cached = self._idempotency_preflight(
            "water.set_pattern", params, workflow_id=wid_hint
        )
        if cached is not None:
            return cached
        workflow_id, _ = self._require_active_workflow(params)
        self._require_live_adjust(params.get("confirmation"))
        if self.activity != "water" or self.phase not in {"running", "paused"}:
            raise BridgeError("pattern adjustment requires running/paused FreeSolo water")
        reserved = self._reserve_request(
            "water.set_pattern", params, workflow_id=workflow_id
        )
        if reserved.get("cached") and reserved.get("status") == IDEM_COMPLETED:
            return dict(reserved.get("result") or {})
        pattern = str(params.get("pattern", ""))
        if pattern not in {"center", "spiral", "circular", "ring"}:
            self._fail_request(request_id, "invalid pattern", keep_pending=False)
            raise BridgeError("pattern must be center, spiral, or circular")
        if pattern == "ring":
            pattern = "circular"
        try:
            event = await self.client.set_water_pattern(pattern)
        except Exception as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"water pattern adjustment outcome is unconfirmed: {exc}"
            self._fail_request(request_id, str(exc), keep_pending=True)
            raise BridgeError(
                f"{self.last_error}; use bridge cancel or the physical control"
            ) from exc
        self.targets["pattern"] = pattern
        firmware = str(self.machine_info.get("firmware") or "")
        verified = firmware in LIVE_PATTERN_VERIFIED_FIRMWARE
        result = {
            "status": self.phase,
            "activity": "water",
            "workflow_id": workflow_id,
            "target_pattern": pattern,
            "report": event.command_code if event is not None else None,
            "report_observed": event is not None,
            "hardware_effect_verified": verified,
            "verified_firmware": firmware if verified else None,
        }
        self._complete_request(request_id, result)
        return result

    async def rpc(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = {} if params is None else dict(params)
        if method in {"ping", "status"}:
            return self.status()
        if method == "events":
            workflow_id = params.get("workflow_id")
            return self.events_since(
                int(params.get("since", 0)),
                workflow_id=str(workflow_id) if workflow_id else None,
            )
        async with self._op_lock:
            if method == "connect":
                return await self._connect_unlocked(params, scope="explicit")
            if method == "disconnect":
                return await self._disconnect_unlocked(reason="explicit")
            if method == "probe":
                return await self._probe(params)
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
                return await self._scale_tare(params)
            if method == "presets.save":
                return await self._save_presets(params)
            if method == "grinder.start":
                return await self._grinder_start(params)
            if method == "water.start":
                return await self._water_start(params)
            if method == "pause":
                return await self._pause(params)
            if method == "resume":
                return await self._resume(params)
            if method in {"stop", "cancel"}:
                return await self._stop(params, rpc_method=method)
            if method == "water.set_temperature":
                return await self._set_water_temperature(params)
            if method == "water.set_pattern":
                return await self._set_water_pattern(params)
            if method in {"recovery.reconcile", "reconcile"}:
                return await self._recovery_reconcile(params)
        raise BridgeError(f"unknown bridge method {method}")

    async def shutdown(self, *, force: bool = False) -> None:
        async with self._op_lock:
            if self.activity is not None:
                if not force:
                    raise BridgeError(
                        f"bridge owns {self.activity}:{self.phase}; stop/cancel before shutdown"
                    )
                await self._stop(
                    {
                        "request_id": f"shutdown_{uuid4().hex}",
                        "emergency": True,
                        "workflow_id": self.active_workflow_id,
                    }
                )
            elif force and (self.coffee_state_file.exists() or self.tea_state_file.exists()):
                await self._stop(
                    {
                        "request_id": f"shutdown_{uuid4().hex}",
                        "emergency": True,
                        "workflow_id": self.active_workflow_id,
                    }
                )
            self._cancel_pending_release()
            self._cancel_idle_orphan_task()
            self._idle_orphan_since = None
            self._idle_orphan_deadline = None
            if self.connected:
                # Force shutdown may still own a recovery activity; drop the
                # link without requiring idle (durable recovery is retained).
                await self._disconnect_unlocked(
                    reason="shutdown",
                    require_idle_activity=not force,
                )
            if force and self.activity is not None:
                # Last resort: clear process-local activity without claiming a
                # successful machine stop or rolling back durable terminal.
                self.activity = None
                self.phase = "disconnected"
                self.targets = {}
            if self._store_owned:
                try:
                    self.store.close()
                except Exception:
                    pass


class BridgeServer:
    """Authenticated loopback JSON-line server with lifecycle lock and hello.

    Lock ownership:
    - When ``lock`` is omitted and ``acquire_lock=True``, :meth:`run` acquires
      the lock and **owns** release on exit (success or failure).
    - When a pre-owned ``lock`` is passed, ownership is explicit via
      ``owns_lock`` (default ``False``): the caller retains release duty and
      :meth:`run` never releases it. Pass ``owns_lock=True`` to transfer
      release responsibility to the server for the remainder of the process.
    - ``acquire_lock=False`` with no lock is for lockless test/legacy harnesses
      only; production serve always holds a lock.
    """

    def __init__(
        self,
        core: BridgeCore,
        *,
        record_path: Path | None = None,
        token: str | None = None,
        lock: BridgeLock | None = None,
        acquire_lock: bool = True,
        owns_lock: bool | None = None,
    ) -> None:
        self.core = core
        self.state_root = normalize_state_root(core.state_dir)
        self.record_path = (
            Path(record_path)
            if record_path is not None
            else bridge_record_path(self.state_root)
        )
        self.token = token or secrets.token_urlsafe(32)
        self.shutdown_event = asyncio.Event()
        self.server: asyncio.Server | None = None
        self._acquire_lock = bool(acquire_lock)
        if lock is not None:
            self.lock = lock
            # Pre-owned lock: default to not releasing on cleanup.
            self._owns_lock = bool(owns_lock) if owns_lock is not None else False
        elif acquire_lock:
            self.lock = BridgeLock(self.state_root)
            # Acquired in run(); owns_lock True after successful acquire.
            self._owns_lock = False if owns_lock is None else bool(owns_lock)
        else:
            self.lock = None
            self._owns_lock = False

    def _public_identity(self) -> dict[str, Any]:
        return {
            "instance_id": self.core.instance_id,
            "pid": os.getpid(),
            "host": BRIDGE_HOST,
            "core_version": self.core.core_version,
            "rpc_protocol_min": RPC_PROTOCOL_MIN,
            "rpc_protocol_max": RPC_PROTOCOL_MAX,
            "rpc_protocol_current": RPC_PROTOCOL_CURRENT,
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "record_format_version": BRIDGE_RECORD_FORMAT_VERSION,
            "config_fingerprint": self.core.config_fingerprint,
            "started_at": self.core.started_at,
        }

    def _hello(self, params: Mapping[str, Any]) -> dict[str, Any]:
        missing = [
            field
            for field in ("client_name", "client_version", "protocol_min", "protocol_max")
            if field not in params or params.get(field) is None
        ]
        if missing:
            raise BridgeError(
                "hello requires declared fields: " + ", ".join(missing)
            )
        client_name = params.get("client_name")
        client_version = params.get("client_version")
        if not isinstance(client_name, str) or not client_name.strip():
            raise BridgeError("hello client_name must be a non-empty string")
        if not isinstance(client_version, str) or not client_version.strip():
            raise BridgeError("hello client_version must be a non-empty string")
        client_name = client_name.strip()
        client_version = client_version.strip()
        client_min, client_max = require_protocol_range(
            params.get("protocol_min"),
            params.get("protocol_max"),
            field_prefix="protocol",
        )
        client_fp = params.get("config_fingerprint")
        if client_fp is not None:
            if not isinstance(client_fp, str) or not client_fp.strip():
                raise BridgeError("hello config_fingerprint must be a non-empty string when set")
            client_fp = client_fp.strip()
        compatibility = evaluate_compatibility(
            client_protocol_min=client_min,
            client_protocol_max=client_max,
            client_config_fingerprint=client_fp,
            server_config_fingerprint=self.core.config_fingerprint,
            strict_protocol_types=True,
        )
        # Always return identity + compatibility; never expose the auth token.
        # Incompatible clients are rejected later on non-diagnostic methods.
        return {
            **self._public_identity(),
            "client_name": client_name,
            "client_version": client_version,
            "compatibility": compatibility,
            "config_match": compatibility.get("config_match"),
            "config_warning": (
                "client config fingerprint differs from daemon startup snapshot"
                if compatibility.get("config_match") is False
                else None
            ),
        }

    def _require_compatible_request(self, request: Mapping[str, Any], method: str) -> None:
        if method in DIAGNOSTIC_METHODS:
            return
        # Prefer envelope fields; fall back to params for convenience.
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        raw_min = request.get("protocol_min", params.get("protocol_min"))
        raw_max = request.get("protocol_max", params.get("protocol_max"))
        if raw_min is None or raw_max is None:
            raise BridgeCompatibilityError(
                "non-diagnostic bridge methods require protocol_min/protocol_max "
                "(call hello first via the client helper)"
            )
        try:
            client_min, client_max = require_protocol_range(raw_min, raw_max)
        except BridgeError as exc:
            # Type/range failures on the envelope are hard errors before dispatch.
            raise BridgeError(str(exc)) from exc
        compatibility = evaluate_compatibility(
            client_protocol_min=client_min,
            client_protocol_max=client_max,
            strict_protocol_types=True,
        )
        if not compatibility["compatible"]:
            raise BridgeCompatibilityError(
                "incompatible bridge client: " + "; ".join(compatibility["reasons"])
            )

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

            if method == "hello":
                # Authenticated handshake only; never reaches BridgeCore.rpc.
                result = self._hello(params)
            elif method == "shutdown":
                self._require_compatible_request(request, method)
                await self.core.shutdown(force=bool(params.get("force", False)))
                result = {"status": "shutting_down"}
                self.shutdown_event.set()
            elif method in {"ping", "status"}:
                # Diagnostics: never require hello; annotate compatibility when asked.
                result = await self.core.rpc(method, params)
                raw_min = request.get("protocol_min", params.get("protocol_min"))
                raw_max = request.get("protocol_max", params.get("protocol_max"))
                if raw_min is not None and raw_max is not None:
                    try:
                        client_min, client_max = require_protocol_range(
                            raw_min, raw_max, require_present=True
                        )
                        result = dict(result)
                        result["compatibility"] = evaluate_compatibility(
                            client_protocol_min=client_min,
                            client_protocol_max=client_max,
                            client_config_fingerprint=(
                                str(params["config_fingerprint"])
                                if params.get("config_fingerprint") is not None
                                else None
                            ),
                            server_config_fingerprint=self.core.config_fingerprint,
                            strict_protocol_types=True,
                        )
                    except BridgeError:
                        # Invalid optional annotation types: omit compatibility.
                        pass
            else:
                # Reject incompatible clients before any BridgeCore.rpc / BLE path.
                self._require_compatible_request(request, method)
                result = await self.core.rpc(method, params)
            # Strip token if a handler ever leaked it.
            if isinstance(result, dict):
                result = {k: v for k, v in result.items() if k != "token"}
            response = {"id": request_id, "ok": True, "result": result}
        except BridgeCompatibilityError as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": "BridgeCompatibilityError",
            }
            category = getattr(exc, "category", None)
            if category:
                response["category"] = str(category)
        except BridgeError as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": type(exc).__name__,
            }
            category = getattr(exc, "category", None)
            if category:
                # Stable wire category (e.g. device_busy_external); never tokens.
                response["category"] = str(category)
        except Exception as exc:
            response = {
                "id": request_id,
                "ok": False,
                "error": str(exc),
                "type": type(exc).__name__,
            }
        writer.write(
            (
                json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n"
            ).encode("utf-8")
        )
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _release_owned_lock(self) -> None:
        if self._owns_lock and self.lock is not None and self.lock.owned:
            self.lock.release()
        self._owns_lock = False

    def _cleanup_own_record(self) -> None:
        try:
            current = _read_json(self.record_path)
        except BridgeError:
            return
        # Never delete a live record owned by another instance.
        if current.get("token") == self.token and current.get(
            "instance_id"
        ) == self.core.instance_id:
            _unlink(self.record_path)

    async def run(self) -> None:
        """Acquire ownership (if configured), bind, publish record, serve.

        Every step after lock acquisition is under a finally that releases the
        OS lock when this server owns it, so bind/record failures cannot leak
        the lock.
        """

        acquired_here = False
        if self.lock is not None and not self.lock.owned:
            if not self.lock.acquire(blocking=False):
                raise BridgeLockError(
                    f"another bridge instance holds {self.lock.path}; not starting"
                )
            acquired_here = True
            self._owns_lock = True

        try:
            # Even with the OS lock held, a lockless/legacy peer may still be
            # answering on a live record. Probe before unlinking.
            # Self-owned only when *both* token and instance_id match -- token
            # collision/reuse must not clobber a responsive different instance.
            # Run the probe in a worker thread so a same-loop test/legacy peer
            # can still accept the diagnostic connection.
            if self.record_path.exists():
                try:
                    stale = _read_json(self.record_path)
                except BridgeError:
                    stale = {}
                is_self_owned = (
                    stale.get("token") == self.token
                    and stale.get("instance_id") == self.core.instance_id
                )
                if not is_self_owned:
                    live = await asyncio.to_thread(
                        _probe_record_responsive, stale, 0.5
                    )
                    if live:
                        raise BridgeError(
                            "a live bridge record is already serving this state root; "
                            "refusing to start a second daemon or delete the live record"
                        )
                    _unlink(self.record_path)

            self.server = await asyncio.start_server(
                self._handle, BRIDGE_HOST, 0, limit=65536
            )
            try:
                socket_info = self.server.sockets[0].getsockname()
                record = {
                    **self._public_identity(),
                    "port": int(socket_info[1]),
                    "token": self.token,
                    "started_at": self.core.started_at,
                }
                _atomic_json(self.record_path, record, private=True)
                await self.shutdown_event.wait()
            finally:
                self.server.close()
                await self.server.wait_closed()
                self._cleanup_own_record()
        finally:
            # Always release a lock we acquired (or were transferred ownership of).
            if acquired_here or self._owns_lock:
                self._release_owned_lock()


# Process-local hello cache: must include every input that affects compatibility.
_hello_ok: dict[str, str] = {}


def _hello_cache_key(
    record: Mapping[str, Any],
    record_path: Path,
    *,
    protocol_min: int,
    protocol_max: int,
    client_name: str,
    client_version: str,
    config_fingerprint_value: str | None,
) -> str:
    return (
        f"{record_path}:{record.get('instance_id')}:{record.get('token')}:"
        f"{protocol_min}:{protocol_max}:{client_name}:{client_version}:"
        f"{config_fingerprint_value or ''}"
    )


def _probe_record_responsive(
    record: Mapping[str, Any],
    timeout: float = 0.5,
) -> bool:
    """Return True if the discovery record answers authenticated diagnostics.

    Uses a blocking socket; callers on an asyncio event loop that also hosts
    the peer must invoke this via ``asyncio.to_thread`` so the peer can accept.
    """

    try:
        host = str(record.get("host") or "")
        if host != BRIDGE_HOST:
            return False
        port = int(record["port"])
        token = str(record.get("token") or "")
        if not token:
            return False
        request = {
            "id": secrets.token_hex(8),
            "token": token,
            "method": "ping",
            "params": {},
        }
        with socket.create_connection((host, port), timeout=float(timeout)) as connection:
            connection.settimeout(float(timeout))
            connection.sendall((json.dumps(request, allow_nan=False) + "\n").encode("utf-8"))
            chunks = bytearray()
            while b"\n" not in chunks:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > 1_000_000:
                    return False
        response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
        return bool(response.get("ok"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError, UnicodeDecodeError, IndexError):
        return False


def _daemon_protocol_info(status_or_record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize protocol fields from status/hello/record (legacy-tolerant)."""

    def _as_int(value: Any, default: int | None = None) -> int | None:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    current = _as_int(
        status_or_record.get("rpc_protocol_current")
        or status_or_record.get("protocol_version")
    )
    pmin = _as_int(status_or_record.get("rpc_protocol_min"), current)
    pmax = _as_int(status_or_record.get("rpc_protocol_max"), current)
    legacy = False
    if current is None and pmin is None and pmax is None:
        # Pre-versioned daemon: treat as protocol 1.
        current, pmin, pmax = 1, 1, 1
        legacy = True
    elif current is not None and current <= LEGACY_RPC_PROTOCOL_MAX:
        legacy = True
        if pmin is None:
            pmin = current
        if pmax is None:
            pmax = current
    elif pmax is not None and pmax < RPC_PROTOCOL_MIN:
        legacy = True
    return {
        "rpc_protocol_min": pmin if pmin is not None else 1,
        "rpc_protocol_max": pmax if pmax is not None else 1,
        "rpc_protocol_current": current if current is not None else 1,
        "legacy": legacy,
        "compatible_with_client": _protocol_overlap(
            RPC_PROTOCOL_MIN,
            RPC_PROTOCOL_MAX,
            pmin if pmin is not None else 1,
            pmax if pmax is not None else 1,
        ),
    }


def bridge_call(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    record_path: Path | None = None,
    client_name: str = "xbloom-studio",
    client_version: str | None = None,
    protocol_min: int = RPC_PROTOCOL_MIN,
    protocol_max: int = RPC_PROTOCOL_MAX,
    require_hello: bool | None = None,
    config_fingerprint_value: str | None = None,
    include_config_fingerprint: bool = True,
    omit_protocol_envelope: bool = False,
) -> dict[str, Any]:
    """Authenticated JSON-line RPC against the local bridge.

    Non-diagnostic methods perform hello/compatibility validation first (unless
    ``require_hello=False``). The auth token is never returned in results.
    Normal clients declare their effective config fingerprint by default.
    """

    path = Path(record_path) if record_path is not None else bridge_record_path()
    record = _read_json(path)
    host = str(record.get("host") or "")
    if host != BRIDGE_HOST:
        raise BridgeError("bridge record does not point to the required loopback host")

    # Public compatibility inputs: strict JSON integers, no float/bool/str coerce.
    pmin, pmax = require_protocol_range(protocol_min, protocol_max)

    if require_hello is None:
        # Diagnostics (hello/ping/status) skip the hello preflight.
        require_hello = method not in DIAGNOSTIC_METHODS

    version = client_version or _core_version()
    client_fp = config_fingerprint_value
    if include_config_fingerprint and client_fp is None:
        client_fp = config_fingerprint()
    cache_key = _hello_cache_key(
        record,
        path,
        protocol_min=pmin,
        protocol_max=pmax,
        client_name=client_name,
        client_version=version,
        config_fingerprint_value=client_fp,
    )

    if require_hello and _hello_ok.get(cache_key) != "ok":
        hello_params: dict[str, Any] = {
            "client_name": client_name,
            "client_version": version,
            "protocol_min": pmin,
            "protocol_max": pmax,
        }
        if client_fp is not None:
            hello_params["config_fingerprint"] = client_fp
        hello_result = _bridge_call_raw(
            "hello",
            hello_params,
            timeout=min(float(timeout), 5.0),
            record=record,
            protocol_min=pmin,
            protocol_max=pmax,
        )
        compatibility = hello_result.get("compatibility") or {}
        if not compatibility.get("compatible", True):
            raise BridgeCompatibilityError(
                "incompatible bridge client: "
                + "; ".join(compatibility.get("reasons") or ["unknown"])
            )
        _hello_ok[cache_key] = "ok"

    call_params = dict(params or {})
    if (
        include_config_fingerprint
        and client_fp is not None
        and "config_fingerprint" not in call_params
        and method in {"status", "ping", "hello"}
    ):
        call_params["config_fingerprint"] = client_fp

    return _bridge_call_raw(
        method,
        call_params,
        timeout=timeout,
        record=record,
        protocol_min=pmin,
        protocol_max=pmax,
        client_name=client_name,
        client_version=version,
        omit_protocol_envelope=omit_protocol_envelope,
        config_fingerprint_value=client_fp if include_config_fingerprint else None,
    )


def _bridge_call_raw(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    record: Mapping[str, Any],
    protocol_min: int = RPC_PROTOCOL_MIN,
    protocol_max: int = RPC_PROTOCOL_MAX,
    client_name: str | None = None,
    client_version: str | None = None,
    omit_protocol_envelope: bool = False,
    config_fingerprint_value: str | None = None,
) -> dict[str, Any]:
    host = str(record.get("host") or BRIDGE_HOST)
    request: dict[str, Any] = {
        "id": secrets.token_hex(8),
        "token": record.get("token"),
        "method": method,
        "params": dict(params or {}),
    }
    if not omit_protocol_envelope:
        pmin, pmax = require_protocol_range(protocol_min, protocol_max)
        request["protocol_min"] = pmin
        request["protocol_max"] = pmax
    if client_name is not None:
        request["client_name"] = client_name
    if client_version is not None:
        request["client_version"] = client_version
    if config_fingerprint_value is not None and method not in DIAGNOSTIC_METHODS:
        # Envelope-level declaration for non-diagnostic RPCs.
        request["config_fingerprint"] = config_fingerprint_value
    try:
        with socket.create_connection(
            (host, int(record["port"])),
            timeout=float(timeout),
        ) as connection:
            connection.settimeout(float(timeout))
            connection.sendall(
                (json.dumps(request, allow_nan=False) + "\n").encode("utf-8")
            )
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
        err_type = str(response.get("type") or "")
        message = str(response.get("error") or "bridge request failed")
        # Never surface raw auth material if a buggy handler leaked it.
        if "token" in message.casefold():
            message = "bridge request failed"
        category_raw = response.get("category")
        category = str(category_raw) if category_raw else None
        if err_type == "BridgeCompatibilityError" or "incompatible" in message.lower():
            raise BridgeCompatibilityError(message, category=category)
        raise BridgeError(message, category=category)
    result = response.get("result")
    if not isinstance(result, dict):
        raise BridgeError("bridge returned a non-object result")
    if "token" in result:
        result = {k: v for k, v in result.items() if k != "token"}
    return result


def bridge_is_running(*, record_path: Path | None = None) -> bool:
    try:
        bridge_call(
            "ping",
            timeout=0.5,
            record_path=record_path,
            require_hello=False,
            include_config_fingerprint=False,
        )
        return True
    except BridgeError:
        return False


def _legacy_shutdown(
    record: Mapping[str, Any],
    *,
    force: bool = False,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Token-authenticated shutdown without hello/envelope (legacy wire)."""

    return _bridge_call_raw(
        "shutdown",
        {"force": bool(force)},
        timeout=timeout,
        record=record,
        omit_protocol_envelope=True,
    )


def _spawn_bridge_process(
    *,
    address: str | None,
    state_root: Path,
    log_path: Path,
    environ: Mapping[str, str] | None = None,
) -> subprocess.Popen[Any]:
    """Spawn the core-owned daemon (no Skill script path required)."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Parent-parser flags must precede the subcommand (argparse).
    command = [sys.executable, "-m", "xbloom_ble.bridge"]
    if address:
        command.extend(["--address", str(address)])
    command.append("serve")
    child_env = environment_copy(environ)
    # Ensure the child uses the same state root.
    child_env["XBLOOM_STATE_DIR"] = str(state_root)
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        popen_kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            env=child_env,
            **popen_kwargs,
        )


def _poll_bridge_status(
    record_path: Path,
    *,
    timeout: float = 8.0,
    log_path: Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + float(timeout)
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        # Bounded polling without long sleeps; short yield only.
        time.sleep(0.05)
        try:
            return bridge_call(
                "status",
                timeout=0.5,
                record_path=record_path,
                require_hello=False,
            )
        except BridgeError as exc:
            last_error = exc
    detail = f": {last_error}" if last_error else ""
    hint = f"; inspect {log_path}" if log_path is not None else ""
    raise BridgeError(f"bridge did not start{hint}{detail}")


def _annotate_config(
    status: dict[str, Any],
    *,
    address: str | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Attach client-effective config fingerprint match fields to a status dict."""

    client_fp = config_fingerprint(environ, address=address)
    server_fp = status.get("config_fingerprint")
    config_match: bool | None
    if server_fp is None:
        config_match = None
    else:
        config_match = str(server_fp) == client_fp
    status["client_config_fingerprint"] = client_fp
    status["config_match"] = config_match
    if config_match is False:
        status["config_warning"] = (
            "client effective config fingerprint differs from the running daemon; "
            "restart only when idle (never forces active work)"
        )
    else:
        status.pop("config_warning", None)
    return status


def _status_is_safely_idle(status: Mapping[str, Any]) -> bool:
    if status.get("activity") is not None:
        return False
    if list(status.get("recovery_records") or []):
        return False
    if status.get("idle") is False:
        return False
    phase = status.get("phase")
    if phase is not None and str(phase) not in SAFE_IDLE_PHASES:
        return False
    return bool(status.get("idle", True))


def _mark_lifecycle_result(
    status: dict[str, Any],
    *,
    client_ready: bool,
    ensured: bool | None = None,
) -> dict[str, Any]:
    """Attach lifecycle contract fields. ``client_ready`` means a confirmed
    protocol-compatible running daemon is available for normal client RPCs.
    """

    status["client_ready"] = bool(client_ready)
    if ensured is not None:
        status["ensured"] = bool(ensured)
    elif "ensured" not in status:
        # Prefer ensured=False when we could not provide a compatible daemon.
        status["ensured"] = bool(client_ready)
    return status


def _upgrade_or_reuse_running_daemon(
    *,
    path: Path,
    root: Path,
    address: str | None,
    environ: Mapping[str, str] | None,
    start_timeout: float,
    started_flag: bool,
) -> dict[str, Any]:
    """Handle a responsive daemon: upgrade legacy/idle-mismatch or annotate reuse."""

    status = bridge_call(
        "status",
        record_path=path,
        require_hello=False,
        include_config_fingerprint=True,
    )
    status = dict(status)
    proto = _daemon_protocol_info(status)
    status = _annotate_config(status, address=address, environ=environ)
    status["protocol_compatible"] = proto["compatible_with_client"]
    status["legacy_daemon"] = proto["legacy"]

    needs_upgrade = proto["legacy"] or not proto["compatible_with_client"]
    config_mismatch = status.get("config_match") is False
    safely_idle = _status_is_safely_idle(status)

    if needs_upgrade:
        if not safely_idle:
            status["started"] = False
            status["already_running"] = True
            status["upgrade_pending"] = True
            status["status"] = "upgrade_pending"
            status["reason"] = "legacy_or_incompatible_daemon_not_idle"
            status["message"] = (
                "running daemon is protocol-incompatible but busy or has recovery; "
                "preserving active work until a clean idle stop"
            )
            # Not client-ready: incompatible, and ensure could not replace it.
            return _mark_lifecycle_result(
                status, client_ready=False, ensured=False
            )
        # Idle legacy/incompatible: token-auth shutdown without hello, then relaunch.
        try:
            record = _read_json(path)
            _legacy_shutdown(record, force=False, timeout=start_timeout)
        except BridgeError as exc:
            status["started"] = False
            status["upgrade_pending"] = True
            status["status"] = "upgrade_pending"
            status["reason"] = "legacy_shutdown_failed"
            status["message"] = str(exc)
            return _mark_lifecycle_result(
                status, client_ready=False, ensured=False
            )
        deadline = time.monotonic() + float(start_timeout)
        while time.monotonic() < deadline and bridge_is_running(record_path=path):
            time.sleep(0.05)
        if bridge_is_running(record_path=path):
            status["started"] = False
            status["upgrade_pending"] = True
            status["status"] = "upgrade_pending"
            status["reason"] = "legacy_stop_pending"
            status["message"] = "legacy daemon did not exit after authenticated shutdown"
            return _mark_lifecycle_result(
                status, client_ready=False, ensured=False
            )
        launched = start_bridge_daemon(
            address=address,
            state_root=root,
            environ=environ,
            start_timeout=start_timeout,
        )
        launched = dict(launched)
        launched["upgraded_from_legacy"] = True
        launched["previous_instance_id"] = status.get("instance_id")
        # start_bridge_daemon already attaches client_ready/ensured.
        return launched

    if config_mismatch and safely_idle:
        # Config mismatch alone may remain client-ready (protocol ok) with warning.
        status["started"] = False
        status["already_running"] = True
        status["idle_restart_recommended"] = True
        status["status"] = "config_mismatch_idle"
        status["message"] = (
            "running daemon config fingerprint differs; daemon is idle -- "
            "call restart-if-idle to apply the client config"
        )
        return _mark_lifecycle_result(status, client_ready=True, ensured=True)

    if config_mismatch and not safely_idle:
        status["started"] = False
        status["already_running"] = True
        status["upgrade_pending"] = False
        status["idle_restart_recommended"] = False
        status["status"] = "config_mismatch_active"
        status["message"] = (
            "running daemon config fingerprint differs but work is active/recovery; "
            "not restarting"
        )
        return _mark_lifecycle_result(status, client_ready=True, ensured=True)

    status["started"] = started_flag
    status["already_running"] = not started_flag
    status["upgrade_pending"] = False
    return _mark_lifecycle_result(status, client_ready=True, ensured=True)


def ensure_bridge_daemon(
    *,
    address: str | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    start_timeout: float = 8.0,
) -> dict[str, Any]:
    """Return status of a running bridge, starting or upgrading one if needed.

    Never force-stops active work. Legacy/incompatible idle daemons are shut down
    with a token-authenticated legacy path, then replaced. Config mismatches are
    surfaced; only idle daemons advertise an idle-restart recommendation.
    """

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else skill_state_dir()
    )
    path = bridge_record_path(root)
    if bridge_is_running(record_path=path):
        return _upgrade_or_reuse_running_daemon(
            path=path,
            root=root,
            address=address,
            environ=environ,
            start_timeout=start_timeout,
            started_flag=False,
        )
    return start_bridge_daemon(
        address=address,
        state_root=root,
        environ=environ,
        start_timeout=start_timeout,
    )


def start_bridge_daemon(
    script_path: Path | str | None = None,
    *,
    address: str | None = None,
    record_path: Path | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    start_timeout: float = 8.0,
) -> dict[str, Any]:
    """Start the core-owned bridge daemon.

    ``script_path`` is accepted for temporary backwards compatibility with
    callers that previously launched ``scripts/xbloom.py bridge serve``. New
    call sites must not depend on a Skill script path; the daemon is always
    spawned as ``python -m xbloom_ble.bridge``.
    """

    if record_path is not None:
        path = Path(record_path)
        root = normalize_state_root(path.parent)
    elif state_root is not None:
        root = normalize_state_root(state_root)
        path = bridge_record_path(root)
    else:
        root = skill_state_dir()
        path = bridge_record_path(root)

    if bridge_is_running(record_path=path):
        return _upgrade_or_reuse_running_daemon(
            path=path,
            root=root,
            address=address,
            environ=environ,
            start_timeout=start_timeout,
            started_flag=False,
        )

    # Probe lock: if another instance holds it, wait briefly for its record.
    # Also refuse to start if a lockless live record answers diagnostics.
    if path.exists():
        try:
            existing = _read_json(path)
        except BridgeError:
            existing = {}
        if existing and _probe_record_responsive(existing, timeout=0.5):
            # Live peer without us holding its lock -- do not race it.
            # Route through the same compatibility/upgrade decision path.
            return _upgrade_or_reuse_running_daemon(
                path=path,
                root=root,
                address=address,
                environ=environ,
                start_timeout=start_timeout,
                started_flag=False,
            )

    probe = BridgeLock(root)
    if not probe.acquire(blocking=False):
        try:
            # Concurrent starter holds the lock; wait for its record then apply
            # the same compatibility normalization (do not bypass upgrade path).
            _poll_bridge_status(path, timeout=start_timeout)
            return _upgrade_or_reuse_running_daemon(
                path=path,
                root=root,
                address=address,
                environ=environ,
                start_timeout=start_timeout,
                started_flag=False,
            )
        except BridgeError as exc:
            raise BridgeError(
                "bridge lock is held but the daemon did not become ready"
            ) from exc
    # We hold the lock in this probe process - release so the child can acquire.
    probe.release()

    log_path = root / BRIDGE_LOG_NAME
    child_env = environment_copy(environ)
    if address:
        # Align child fingerprint with requested address.
        child_env.setdefault("XBLOOM_ADDRESS", str(address))
    _spawn_bridge_process(
        address=address,
        state_root=root,
        log_path=log_path,
        environ=child_env,
    )
    status = _poll_bridge_status(path, timeout=start_timeout, log_path=log_path)
    status = dict(status)
    status = _annotate_config(status, address=address, environ=child_env)
    proto = _daemon_protocol_info(status)
    status["protocol_compatible"] = proto["compatible_with_client"]
    status["legacy_daemon"] = proto["legacy"]
    status["started"] = True
    status["already_running"] = False
    status["upgrade_pending"] = False
    ready = bool(
        status.get("running")
        and proto["compatible_with_client"]
        and not proto["legacy"]
    )
    _mark_lifecycle_result(status, client_ready=ready, ensured=ready)
    # script_path is intentionally unused for spawning (compat signature only).
    _ = script_path
    return status


def stop_bridge_daemon(
    *,
    force: bool = False,
    state_root: Path | str | None = None,
    record_path: Path | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Request a clean shutdown of the running bridge daemon.

    Compatible daemons use hello + envelope. Legacy/incompatible idle daemons
    use a token-authenticated shutdown without hello so an upgrade can proceed.
    Active legacy work is never force-stopped by the upgrade path (``force`` only
    applies to the daemon's own shutdown semantics for an active activity).
    """

    if record_path is not None:
        path = Path(record_path)
    else:
        root = (
            normalize_state_root(state_root)
            if state_root is not None
            else skill_state_dir()
        )
        path = bridge_record_path(root)
    if not bridge_is_running(record_path=path):
        return {"running": False, "status": "already_stopped"}

    record = _read_json(path)
    status: dict[str, Any] = {}
    try:
        status = bridge_call(
            "status",
            record_path=path,
            require_hello=False,
            include_config_fingerprint=False,
            timeout=min(float(timeout), 2.0),
        )
    except BridgeError:
        status = {}
    proto = _daemon_protocol_info(status or record)
    use_legacy = proto["legacy"] or not proto["compatible_with_client"]

    if use_legacy:
        result = _legacy_shutdown(record, force=force, timeout=timeout)
    else:
        result = bridge_call(
            "shutdown",
            {"force": bool(force)},
            record_path=path,
            timeout=timeout,
            require_hello=True,
        )
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if not bridge_is_running(record_path=path):
            out = dict(result)
            out["running"] = False
            out["status"] = "stopped"
            return out
        time.sleep(0.05)
    return {**dict(result), "running": True, "status": "stop_pending"}


def restart_bridge_daemon_if_idle(
    *,
    address: str | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Restart only when the daemon is idle and has no recovery records.

    ``force`` is rejected: it must never bypass active work or recovery.
    ``restarted`` is True only after the old instance is confirmed gone and a
    new instance is confirmed healthy; otherwise status is pending/failure.
    """

    if force:
        return {
            "restarted": False,
            "status": "force_rejected",
            "reason": "force_not_supported",
            "message": (
                "restart-if-idle never force-stops active work or recovery; "
                "omit force and wait until the daemon is idle"
            ),
        }

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else skill_state_dir()
    )
    path = bridge_record_path(root)
    if not bridge_is_running(record_path=path):
        started = start_bridge_daemon(
            address=address, state_root=root, environ=environ
        )
        if started.get("running") and started.get("instance_id"):
            return {
                **started,
                "restarted": True,
                "reason": "was_not_running",
            }
        return {
            **dict(started),
            "restarted": False,
            "status": "start_failed",
            "reason": "was_not_running_start_unconfirmed",
        }

    status = bridge_call(
        "status",
        record_path=path,
        require_hello=False,
        include_config_fingerprint=True,
    )
    idle = bool(status.get("idle"))
    recovery = list(status.get("recovery_records") or [])
    activity = status.get("activity")
    if activity is not None or not idle or recovery or not _status_is_safely_idle(status):
        return {
            "restarted": False,
            "status": "upgrade_pending",
            "reason": "bridge_not_idle",
            "activity": activity,
            "idle": idle,
            "recovery_records": recovery,
            "instance_id": status.get("instance_id"),
            "config_fingerprint": status.get("config_fingerprint"),
            "message": (
                "bridge is busy or has recovery records; "
                "config/upgrade will apply after a clean idle stop"
            ),
        }

    previous_id = status.get("instance_id")
    stop_result = stop_bridge_daemon(state_root=root, record_path=path)
    if stop_result.get("status") != "stopped" or stop_result.get("running"):
        return {
            "restarted": False,
            "status": stop_result.get("status") or "stop_pending",
            "reason": "old_instance_not_confirmed_gone",
            "previous_instance_id": previous_id,
            "stop_result": stop_result,
            "message": "old bridge instance did not confirm exit; not starting a replacement",
        }

    started = start_bridge_daemon(
        address=address, state_root=root, environ=environ
    )
    if not (
        started.get("running")
        and started.get("instance_id")
        and bridge_is_running(record_path=path)
    ):
        return {
            **dict(started),
            "restarted": False,
            "status": "start_failed",
            "reason": "new_instance_not_confirmed_healthy",
            "previous_instance_id": previous_id,
            "message": "old instance stopped but replacement was not confirmed healthy",
        }
    return {
        **started,
        "restarted": True,
        "previous_instance_id": previous_id,
    }


async def serve_bridge(
    *,
    address: str | None = None,
    state_root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    acquire_lock: bool = True,
) -> None:
    """Run the bridge server, holding the lifecycle lock for the process."""

    root = (
        normalize_state_root(state_root)
        if state_root is not None
        else skill_state_dir()
    )
    core = BridgeCore(
        default_address=address,
        state_dir=root,
        environ=environ,
    )
    server = BridgeServer(core, acquire_lock=acquire_lock)
    await server.run()


def main(argv: list[str] | None = None) -> None:
    """Console entry for the core-owned ``xbloom-bridge`` daemon."""

    import argparse

    parser = argparse.ArgumentParser(
        prog="xbloom-bridge",
        description=(
            "Run or manage the loopback-only xBloom Studio BLE bridge daemon. "
            "Binds to 127.0.0.1, writes bridge.json under XBLOOM_STATE_DIR "
            "(or legacy XBLOOM_SKILL_STATE_DIR), and holds bridge.lock for "
            "single-instance ownership."
        ),
    )
    parser.add_argument(
        "--address",
        default=None,
        help="preferred BLE address/identifier; defaults to XBLOOM_ADDRESS or scan",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the daemon in the foreground (default)")
    sub.add_parser("start", help="ensure a background daemon is running")
    stop_p = sub.add_parser("stop", help="stop a running daemon")
    stop_p.add_argument(
        "--force",
        action="store_true",
        help="force stop even if an activity is active",
    )
    sub.add_parser("status", help="print daemon status JSON")
    sub.add_parser(
        "restart-if-idle",
        help="restart only when idle with no recovery records",
    )

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "serve":
        asyncio.run(serve_bridge(address=args.address))
        return
    if command == "start":
        result = ensure_bridge_daemon(address=args.address)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if command == "stop":
        result = stop_bridge_daemon(force=bool(getattr(args, "force", False)))
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if command == "status":
        path = bridge_record_path()
        if not bridge_is_running(record_path=path):
            result = {"running": False, "record": str(path)}
        else:
            result = bridge_call("status", record_path=path, require_hello=False)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if command == "restart-if-idle":
        result = restart_bridge_daemon_if_idle(address=args.address)
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    parser.error(f"unknown command {command}")


if __name__ == "__main__":
    main()

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


import xbloom_ble._bridge_daemon as _daemon
import xbloom_ble._bridge_primitives as _primitives
from ._bridge_reconcile import BridgeCoreReconcileMixin
from ._bridge_tools import BridgeCoreToolsMixin
from ._bridge_workflows import BridgeCoreWorkflowsMixin

for _mod in (_primitives, _daemon):
    for _k, _v in vars(_mod).items():
        if not _k.startswith("__"):
            globals()[_k] = _v


class BridgeCore(
    BridgeCoreReconcileMixin,
    BridgeCoreToolsMixin,
    BridgeCoreWorkflowsMixin,
):
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
        # after terminal release so they cannot rewrite terminal -> recovery.
        self._client_generation: int = 0
        self._bound_client_generation: int = 0
        # Reconstruct durable workflow identity without auto-connect / start.
        self._reconstruct_from_store()

    @property
    def coffee_state_file(self) -> Path:
        """Legacy path name only (import-only via migrate). Runtime never reads/writes it."""

        return self.state_dir / COFFEE_STATE_NAME

    @property
    def tea_state_file(self) -> Path:
        """Legacy path name only (import-only via migrate). Runtime never reads/writes it."""

        return self.state_dir / TEA_STATE_NAME

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
        # Hydrate address for later explicit reconnect/cancel only -- never
        # auto-connect. Prefer durable machine_address, then metadata.address,
        # then the active recovery payload address (migrated armed/tea JSON).
        recovery = active.get("recovery")
        recovery_addr = None
        if isinstance(recovery, Mapping):
            recovery_addr = recovery.get("address")
        chosen_address: str | None = None
        for candidate in (
            meta.get("machine_address"),
            meta.get("address"),
            recovery_addr,
        ):
            if isinstance(candidate, str) and candidate:
                chosen_address = candidate
                break
        if chosen_address:
            self.address = chosen_address
            if not self.default_address:
                self.default_address = chosen_address
        # Remain disconnected; connection_scope stays None until explicit reconnect.



if __name__ == "__main__":
    main()

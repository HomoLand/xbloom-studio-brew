"""Typed Skill/Web-facing client for the long-lived bridge daemon (Phase A9).

This is the normal application API for hardware control. Callers use explicit
methods (``coffee_load``, ``status``, …) rather than an arbitrary RPC pass-
through. Protocol-v3 mutating methods always carry a ``request_id`` (generated
when omitted); workflow-bound methods require an explicit ``workflow_id``.

Uncertain operations are never auto-retried. ``status`` / ``events`` are
read-only and never initiate BLE.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from .bridge import (
    MUTATING_METHODS,
    WORKFLOW_BOUND_METHODS,
    BridgeCompatibilityError,
    BridgeError,
    bridge_call,
    bridge_is_running,
    bridge_record_path,
    ensure_bridge_daemon,
)

__all__ = [
    "TypedBridgeClient",
    "new_request_id",
]


def new_request_id(prefix: str = "req") -> str:
    """Return a fresh protocol-v3 request id."""

    return f"{prefix}_{uuid4().hex}"


class TypedBridgeClient:
    """High-level typed RPC client that owns daemon ensure + hello compatibility.

    Construction does not start the daemon. Hardware methods run a cheap
    :meth:`ensure_daemon` check on every call (reuses a running daemon; does
    not reconnect BLE). ``status`` / ``events`` / ``disconnect`` only address
    an existing daemon and never start one.
    """

    def __init__(
        self,
        *,
        address: str | None = None,
        state_root: Path | str | None = None,
        client_name: str = "xbloom-skill-cli",
        client_version: str | None = None,
        default_timeout: float = 60.0,
        ensure_timeout: float = 8.0,
        auto_ensure: bool = True,
    ) -> None:
        self.address = address
        self.state_root = (
            Path(state_root) if state_root is not None else None
        )
        self.client_name = client_name
        self.client_version = client_version
        self.default_timeout = float(default_timeout)
        self.ensure_timeout = float(ensure_timeout)
        self.auto_ensure = bool(auto_ensure)

    def _record_path(self) -> Path | None:
        """Canonical bridge.json path for this client's state root."""

        if self.state_root is None:
            return None
        return bridge_record_path(self.state_root)

    # ------------------------------------------------------------------
    # Daemon lifecycle
    # ------------------------------------------------------------------
    def ensure_daemon(self) -> dict[str, Any]:
        """Start or reuse a protocol-compatible bridge daemon.

        Any explicit ``client_ready=False`` is rejected before RPCs. Protocol
        / upgrade incompatibilities raise :class:`BridgeCompatibilityError`.
        This never reconnects BLE; it only ensures a usable daemon process.
        """

        result = ensure_bridge_daemon(
            address=self.address,
            state_root=self.state_root,
            start_timeout=self.ensure_timeout,
        )
        out = dict(result)
        if out.get("client_ready") is False:
            reason = str(
                out.get("reason")
                or out.get("status")
                or out.get("message")
                or "daemon_not_client_ready"
            )
            message = str(
                out.get("message")
                or out.get("reason")
                or "bridge daemon is not client-ready"
            )
            # Protocol / legacy / upgrade blockers are compatibility failures.
            compatibility_markers = (
                "upgrade",
                "legacy",
                "incompatible",
                "protocol",
                "config_mismatch",
            )
            is_compat = any(m in reason.casefold() for m in compatibility_markers) or any(
                m in message.casefold() for m in compatibility_markers
            )
            category = "daemon_not_client_ready"
            if is_compat:
                category = "protocol_incompatible"
                raise BridgeCompatibilityError(message, category=category)
            raise BridgeError(message, category=category)
        return out

    def _ensure_for_hardware(self) -> None:
        """Cheap per-RPC ensure for long-lived clients (no readiness cache)."""

        if self.auto_ensure:
            self.ensure_daemon()

    def _with_address(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.address and "address" not in params:
            params["address"] = self.address
        return params

    def _request_id(self, request_id: str | None) -> str:
        rid = (request_id or "").strip() if request_id is not None else ""
        return rid or new_request_id()

    def _call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        require_hello: bool | None = None,
        ensure: bool = True,
        mutating: bool | None = None,
        workflow_bound: bool | None = None,
    ) -> dict[str, Any]:
        """Internal RPC. Prefer the typed public methods.

        Never retries machine mutations. Exact ``request_id`` values are
        preserved when the caller supplies them.
        """

        if ensure:
            self._ensure_for_hardware()
        body = dict(params or {})
        is_mutating = method in MUTATING_METHODS if mutating is None else mutating
        is_bound = (
            method in WORKFLOW_BOUND_METHODS
            if workflow_bound is None
            else workflow_bound
        )
        if is_mutating:
            # Preserve caller-supplied request_id for exact retry/idempotency.
            body["request_id"] = self._request_id(body.get("request_id"))
        if is_bound:
            wid = body.get("workflow_id")
            if wid is None or not str(wid).strip():
                raise BridgeError(
                    f"{method} requires an explicit workflow_id "
                    "(do not invent one; use the load response or status)"
                )
            body["workflow_id"] = str(wid).strip()
        return bridge_call(
            method,
            body,
            timeout=self.default_timeout if timeout is None else float(timeout),
            record_path=self._record_path(),
            client_name=self.client_name,
            client_version=self.client_version,
            require_hello=require_hello,
        )

    # ------------------------------------------------------------------
    # Read-only (never ensure BLE connect; may talk to running daemon only)
    # ------------------------------------------------------------------
    def status(self, *, require_hello: bool = False) -> dict[str, Any]:
        """Read bridge status. Does not ensure daemon or connect BLE."""

        return self._call(
            "status",
            ensure=False,
            require_hello=require_hello,
            mutating=False,
            workflow_bound=False,
        )

    def ping(self) -> dict[str, Any]:
        return self._call(
            "ping",
            ensure=False,
            require_hello=False,
            mutating=False,
            workflow_bound=False,
        )

    def events(
        self,
        *,
        since: int = 0,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll durable/live events. Never connects BLE."""

        params: dict[str, Any] = {"since": int(since)}
        if workflow_id is not None:
            params["workflow_id"] = str(workflow_id)
        return self._call(
            "events",
            params,
            ensure=False,
            require_hello=True,
            mutating=False,
            workflow_bound=False,
        )

    # ------------------------------------------------------------------
    # Explicit connection (debug hold)
    # ------------------------------------------------------------------
    def connect(
        self,
        *,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {"scan_timeout": float(scan_timeout)}
        )
        if address:
            params["address"] = address
        return self._call("connect", params, mutating=False, workflow_bound=False)

    def disconnect(self) -> dict[str, Any]:
        """Release an explicit debug link on an *existing* daemon only.

        Never starts a missing daemon. Fails clearly when no bridge is running.
        """

        record = self._record_path()
        if not bridge_is_running(record_path=record):
            raise BridgeError(
                "no running bridge daemon to disconnect; "
                "start the daemon first or use a hardware command that ensures it",
                category="daemon_not_running",
            )
        return self._call(
            "disconnect",
            ensure=False,
            mutating=False,
            workflow_bound=False,
        )

    # ------------------------------------------------------------------
    # Probe / settings / advanced / presets
    # ------------------------------------------------------------------
    def probe(
        self,
        *,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        """One-shot redacted machine probe via BridgeCore (never direct Bleak)."""

        params = self._with_address({"scan_timeout": float(scan_timeout)})
        if address:
            params["address"] = address
        return self._call(
            "probe", params, mutating=False, workflow_bound=False
        )

    def settings_read(
        self,
        *,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address({"scan_timeout": float(scan_timeout)})
        if address:
            params["address"] = address
        return self._call(
            "settings.read", params, mutating=False, workflow_bound=False
        )

    def settings_write(
        self,
        *,
        confirmation: str,
        request_id: str | None = None,
        weight_unit: str | None = None,
        temperature_unit: str | None = None,
        water_source: str | None = None,
        display: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "confirmation": confirmation,
                "request_id": request_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if weight_unit is not None:
            params["weight_unit"] = weight_unit
        if temperature_unit is not None:
            params["temperature_unit"] = temperature_unit
        if water_source is not None:
            params["water_source"] = water_source
        if display is not None:
            params["display"] = display
        if address:
            params["address"] = address
        return self._call("settings.write", params)

    def advanced_read(
        self,
        *,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address({"scan_timeout": float(scan_timeout)})
        if address:
            params["address"] = address
        return self._call(
            "advanced.read", params, mutating=False, workflow_bound=False
        )

    def advanced_write(
        self,
        *,
        confirmation: str,
        request_id: str | None = None,
        pour_radius_level: int | None = None,
        vibration_level: int | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "confirmation": confirmation,
                "request_id": request_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if pour_radius_level is not None:
            params["pour_radius_level"] = pour_radius_level
        if vibration_level is not None:
            params["vibration_level"] = vibration_level
        if address:
            params["address"] = address
        return self._call("advanced.write", params)

    def presets_save(
        self,
        *,
        recipes: list[str],
        scale: list[bool] | bool = True,
        request_id: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "recipes": list(recipes),
                "scale": scale,
                "request_id": request_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if address:
            params["address"] = address
        return self._call("presets.save", params)

    # ------------------------------------------------------------------
    # Coffee / tea
    # ------------------------------------------------------------------
    def coffee_load(
        self,
        *,
        recipe: str | None = None,
        request_id: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
        recipe_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Load a coffee recipe by local path and/or durable revision id.

        Browser/Web callers must pass ``recipe_revision_id`` only (no local
        path). Skill/MCP keep path-only and path-plus-revision compatibility.
        Revision-only RPC params omit the ``recipe`` key entirely.
        """

        has_recipe = recipe is not None and str(recipe).strip() != ""
        has_rev = (
            recipe_revision_id is not None
            and str(recipe_revision_id).strip() != ""
        )
        if not has_recipe and not has_rev:
            raise BridgeError(
                "coffee.load requires a local recipe path or recipe_revision_id",
                category="invalid_request",
            )
        params: dict[str, Any] = {
            "request_id": request_id,
            "scan_timeout": float(scan_timeout),
        }
        # Revision-only: omit recipe key so bridge never sees a fake path.
        if has_recipe:
            params["recipe"] = str(recipe)
        if has_rev:
            params["recipe_revision_id"] = str(recipe_revision_id).strip()
        if address:
            params["address"] = address
        return self._call("coffee.load", self._with_address(params))

    def coffee_start(
        self,
        *,
        workflow_id: str,
        confirmation: str,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "coffee.start",
            {
                "workflow_id": workflow_id,
                "confirmation": confirmation,
                "request_id": request_id,
            },
            timeout=timeout,
        )

    def tea_load(
        self,
        *,
        recipe: str | None = None,
        request_id: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
        recipe_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Load a tea recipe by local path and/or durable revision id.

        Browser/Web callers must pass ``recipe_revision_id`` only (no local
        path). Skill/MCP keep path-only and path-plus-revision compatibility.
        Revision-only RPC params omit the ``recipe`` key entirely.
        """

        has_recipe = recipe is not None and str(recipe).strip() != ""
        has_rev = (
            recipe_revision_id is not None
            and str(recipe_revision_id).strip() != ""
        )
        if not has_recipe and not has_rev:
            raise BridgeError(
                "tea.load requires a local recipe path or recipe_revision_id",
                category="invalid_request",
            )
        params: dict[str, Any] = {
            "request_id": request_id,
            "scan_timeout": float(scan_timeout),
        }
        if has_recipe:
            params["recipe"] = str(recipe)
        if has_rev:
            params["recipe_revision_id"] = str(recipe_revision_id).strip()
        if address:
            params["address"] = address
        return self._call("tea.load", self._with_address(params))

    def tea_start(
        self,
        *,
        workflow_id: str,
        confirmation: str,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "tea.start",
            {
                "workflow_id": workflow_id,
                "confirmation": confirmation,
                "request_id": request_id,
            },
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # FreeSolo / control
    # ------------------------------------------------------------------
    def scale_start(
        self,
        *,
        duration_s: float,
        tare: bool = False,
        request_id: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "duration_s": float(duration_s),
                "tare": bool(tare),
                "request_id": request_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if address:
            params["address"] = address
        return self._call("scale.start", params)

    def scale_tare(
        self,
        *,
        workflow_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "scale.tare",
            {"workflow_id": workflow_id, "request_id": request_id},
        )

    def grinder_start(
        self,
        *,
        size: int,
        rpm: int,
        seconds: float,
        confirmation: str,
        request_id: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "size": int(size),
                "rpm": int(rpm),
                "seconds": float(seconds),
                "confirmation": confirmation,
                "request_id": request_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if address:
            params["address"] = address
        return self._call("grinder.start", params)

    def water_start(
        self,
        *,
        volume_ml: float,
        temp_c: int,
        confirmation: str,
        flow_ml_s: float = 3.5,
        pattern: str = "center",
        water_source: str = "auto",
        request_id: str | None = None,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "volume_ml": float(volume_ml),
                "temp_c": int(temp_c),
                "flow_ml_s": float(flow_ml_s),
                "pattern": pattern,
                "water_source": water_source,
                "confirmation": confirmation,
                "request_id": request_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if address:
            params["address"] = address
        return self._call("water.start", params)

    def pause(
        self,
        *,
        workflow_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "pause",
            {"workflow_id": workflow_id, "request_id": request_id},
        )

    def resume(
        self,
        *,
        workflow_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "resume",
            {"workflow_id": workflow_id, "request_id": request_id},
        )

    def stop(
        self,
        *,
        workflow_id: str | None = None,
        request_id: str | None = None,
        emergency: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "request_id": request_id,
            "emergency": bool(emergency),
        }
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        # Emergency may omit workflow_id; normal stop requires it.
        return self._call(
            "stop",
            params,
            workflow_bound=not emergency,
        )

    def cancel(
        self,
        *,
        workflow_id: str | None = None,
        request_id: str | None = None,
        emergency: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "request_id": request_id,
            "emergency": bool(emergency),
        }
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        return self._call(
            "cancel",
            params,
            workflow_bound=not emergency,
        )

    def recovery_reconcile(
        self,
        *,
        workflow_id: str,
        address: str | None = None,
        scan_timeout: float = 8.0,
    ) -> dict[str, Any]:
        params = self._with_address(
            {
                "workflow_id": workflow_id,
                "scan_timeout": float(scan_timeout),
            }
        )
        if address:
            params["address"] = address
        return self._call(
            "recovery.reconcile",
            params,
            mutating=False,
            workflow_bound=True,
        )

    def water_set_temperature(
        self,
        *,
        workflow_id: str,
        temp_c: int,
        confirmation: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "water.set_temperature",
            {
                "workflow_id": workflow_id,
                "temp_c": int(temp_c),
                "confirmation": confirmation,
                "request_id": request_id,
            },
        )

    def water_set_pattern(
        self,
        *,
        workflow_id: str,
        pattern: str,
        confirmation: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._call(
            "water.set_pattern",
            {
                "workflow_id": workflow_id,
                "pattern": pattern,
                "confirmation": confirmation,
                "request_id": request_id,
            },
        )

    # ------------------------------------------------------------------
    # Workflow helpers for CLI
    # ------------------------------------------------------------------
    def resolve_active_workflow_id(
        self,
        *,
        explicit: str | None = None,
        kind: str | None = None,
        allowed_phases: set[str] | None = None,
    ) -> str:
        """Return explicit workflow_id or the durable active one with guards."""

        if explicit is not None and str(explicit).strip():
            return str(explicit).strip()
        status = self.status(require_hello=False)
        wid = status.get("active_workflow_id")
        if not wid:
            raise BridgeError("no active workflow; load a recipe first")
        workflow = status.get("workflow") or {}
        if kind is not None:
            wkind = str(workflow.get("kind") or status.get("activity") or "")
            if wkind not in {kind, f"{kind}_recovery"} and status.get("activity") != kind:
                raise BridgeError(
                    f"active workflow kind {wkind!r} does not match required {kind!r}"
                )
        if allowed_phases is not None:
            phase = str(
                status.get("phase")
                or workflow.get("state")
                or workflow.get("machine_phase")
                or ""
            )
            if phase not in allowed_phases:
                raise BridgeError(
                    f"active workflow phase {phase!r} is not in {sorted(allowed_phases)}"
                )
        return str(wid)

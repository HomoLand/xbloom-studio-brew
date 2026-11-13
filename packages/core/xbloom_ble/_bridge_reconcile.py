"""Idempotency, recovery reconciliation, and BLE connection lifecycle mixin."""

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

from ._bridge_import_bridge import *  # noqa: F401,F403


class BridgeCoreReconcileMixin:
    """Idempotency, recovery reconciliation, and connection lifecycle methods."""

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

        # Query fresh state only -- no load, start, or control writes.
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

        # Loaded tea: no positive protocol marker -> remain recovery_required.
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

        # Loaded coffee: fresh armed -> reattach monitoring, clear recovery after durable.
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

    def recovery_record_names(self) -> list[str]:
        """Legacy file-marker names for protocol compatibility.

        Coffee/tea/grinder ``*-state.json`` files are import-only and never
        consulted at runtime. Durable recovery is reflected via activity,
        ``active_workflow_id``, ``recovery``, and ``grinder_guard``. Always
        returns an empty list.
        """

        return []

    def is_idle(self) -> bool:
        """True when phase is safe, no activity, and no durable ownership/recovery."""

        if self.activity is not None:
            return False
        if self.phase not in SAFE_IDLE_PHASES:
            return False
        if self.active_workflow_id is not None:
            return False
        try:
            if self.store.get_active_workflow() is not None:
                return False
        except StorageError:
            return False
        if self._recovery_required:
            return False
        # Cooldown alone does not block idle restart (no active ownership).
        return True

    def has_recovery_records(self) -> bool:
        return bool(self.recovery_record_names())

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
        Recovery may only reconnect/query/reconcile later via explicit RPC --
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
            # Tea has no positive loaded marker -- fail-closed.
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
                    # running/paused -- keep prior non-terminal state when active.
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
        self.machine_info.clear()
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
            # One connect attempt only -- no retry / preemption / background reconnect.
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
                # Retry only the harmless handshake, never a load/start command.
                await client.open_session()
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

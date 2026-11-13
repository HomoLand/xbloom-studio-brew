"""Telemetry event loop, coffee/tea workflows, live control, and RPC dispatch."""

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


class BridgeCoreWorkflowsMixin:
    """Event stream, coffee/tea workflows, pause/resume/stop, and RPC dispatch."""

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
        # Coffee/tea/grinder legacy JSON is import-only; never unlink or rewrite it.
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
        grinder_guard = self.grinder_guard()
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
            "grinder_guard": grinder_guard,
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
                str(workflow_id), since_seq=since, limit=256
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
        # Durable workflow/snapshot is authoritative. Never dual-write
        # armed-state.json (import-only legacy file).
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
                "serving_kind": summary["kind"],
                "firmware": firmware,
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

    def _coffee_start_display_fields(self, workflow_id: str) -> dict[str, Any]:
        """Recipe display fields from durable workflow / in-memory targets only.

        Never reopens or re-hashes the original source path.
        """

        wf = self.store.get_workflow(workflow_id) or {}
        meta = dict(wf.get("metadata") or {})
        targets_meta = dict(meta.get("targets") or {})
        machine_program = (
            self.targets.get("machine_program")
            or targets_meta.get("machine_program")
            or "coffee-pour-over"
        )
        if "manual_preload_ice_g" in self.targets:
            ice = self.targets.get("manual_preload_ice_g")
        else:
            ice = targets_meta.get("manual_preload_ice_g", 0)
        return {
            "machine_program": machine_program,
            "machine_dispenses_ice": False,
            "manual_preload_ice_g": int(ice or 0),
            "workflow_id": workflow_id,
            "snapshot_sha256": wf.get("snapshot_sha256"),
        }

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
        # Durable workflow/snapshot is authoritative. Source YAML changes or
        # deletion after load must not affect start. Never re-read/re-hash path.
        display = self._coffee_start_display_fields(workflow_id)
        if not self.connected:
            await self._ensure_connected(params, scope="workflow")
        if self._loaded_needs_reconcile:
            await self._reconcile_loaded_machine_state(
                kind="coffee", request_id=request_id
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
            event = await self.client.start()
        except BaseException as exc:
            self.phase = "control_unconfirmed"
            self.last_error = f"coffee start outcome is unconfirmed: {exc}"
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
                "machine_program": display.get("machine_program", "coffee-pour-over"),
                "machine_dispenses_ice": False,
                "manual_preload_ice_g": int(
                    display.get("manual_preload_ice_g", 0) or 0
                ),
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
            "machine_program": display.get("machine_program", "coffee-pour-over"),
            "machine_dispenses_ice": False,
            "manual_preload_ice_g": int(
                display.get("manual_preload_ice_g", 0) or 0
            ),
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
        # Durable workflow/snapshot is authoritative. Never dual-write
        # tea-loaded-state.json (import-only legacy file).
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
                "firmware": firmware,
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
        # Durable workflow/snapshot is authoritative. Source YAML changes or
        # deletion after load must not affect start. Never re-read/re-hash path.
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
        connect_params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.phase = "stopping"
        self._cancel_grinder_timer()
        # Explicit cancel/stop after daemon restart must reconnect once when
        # disconnected, using durable machine_address / recovery address.
        if not self.connected:
            await self._ensure_connected(
                dict(connect_params or {}),
                scope="one-shot",
            )
        try:
            event = await self.client.stop_grinder_session()
        except Exception as exc:
            self.phase = "stop_unconfirmed"
            self.last_error = f"grinder STOP/QUIT is unconfirmed: {exc}"
            self._set_workflow_state(
                "stop_unconfirmed",
                workflow_id=workflow_id or self.active_workflow_id,
                recovery={
                    "reason": "grinder_stop_unconfirmed",
                    "error": str(exc),
                },
            )
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "grinder_stop_unconfirmed",
                "workflow_id": workflow_id or self.active_workflow_id,
                "error": str(exc),
            }
            if request_id:
                self._fail_request(request_id, str(exc), keep_pending=True)
            # Unconfirmed stop retains active recovery and connection (no release).
            raise BridgeError(self.last_error) from exc
        cooldown = self._grinder_cooldown_fields()
        result = {
            "status": "stopped",
            "activity": "grinder",
            "ack": event.command_code,
            "workflow_id": workflow_id or self.active_workflow_id,
            "rest_seconds": GRINDER_REST_SECONDS,
            **cooldown,
        }
        if emergency:
            result["emergency"] = True
        self._finish_activity(
            reason,
            release_reason="grinder_confirmed_stop",
            emergency=emergency,
            request_id=request_id,
            idempotency_result=result if request_id else None,
            **cooldown,
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
        # Stale legacy coffee/tea JSON without explicit migration is ignored.
        # Ownership is durable workflow / in-memory activity only.
        if self.activity is None:
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
                connect_params=params,
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
            # Legacy coffee/tea JSON is never a shutdown gate (import-only).
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


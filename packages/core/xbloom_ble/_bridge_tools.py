"""Grinder guard, preflight, settings/advanced, and FreeSolo tool methods."""

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


class BridgeCoreToolsMixin:
    """Grinder cooldown guard, settings/advanced tuning, and FreeSolo tools."""

    @staticmethod
    def _cooldown_until_from_mapping(payload: Mapping[str, Any] | None) -> float | None:
        """Extract a cooldown epoch from recovery/terminal payloads.

        ``blocked_until`` / ``grinder_cooldown_until`` / ``cooldown_until`` are
        authoritative when present. Otherwise a numeric ``stopped_at`` /
        ``grinder_stopped_at`` derives ``stopped_at + GRINDER_REST_SECONDS`` so
        migrated terminal imports without an explicit until still enforce the
        remaining 60-second rest. Unknown/no-timestamp terminals yield None
        (ready: no defensible deadline).
        """

        if not isinstance(payload, Mapping):
            return None
        for key in (
            "grinder_cooldown_until",
            "blocked_until",
            "cooldown_until",
        ):
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        for key in ("grinder_stopped_at", "stopped_at"):
            raw = payload.get(key)
            if raw is None:
                continue
            try:
                stopped = float(raw)
            except (TypeError, ValueError):
                continue
            if stopped > 0:
                return stopped + float(GRINDER_REST_SECONDS)
        return None

    def _grinder_cooldown_until_from_workflow(
        self, workflow: Mapping[str, Any]
    ) -> float | None:
        """Resolve cooldown epoch from recovery payload or latest terminal event."""

        until = self._cooldown_until_from_mapping(
            workflow.get("recovery") if isinstance(workflow.get("recovery"), Mapping) else None
        )
        if until is not None:
            return until
        workflow_id = str(workflow.get("workflow_id") or "")
        if not workflow_id:
            return None
        # Do not catch StorageError here: let it reach grinder_guard so public
        # state becomes unavailable (fail closed). Successful reads with no
        # defensible timestamp still return None -> ready.
        events = self.store.list_workflow_events(workflow_id)
        for event in reversed(events):
            if str(event.get("event_type") or "") != "terminal":
                continue
            payload = event.get("payload")
            until = self._cooldown_until_from_mapping(
                payload if isinstance(payload, Mapping) else None
            )
            if until is not None:
                return until
            break
        return None

    def grinder_guard(self) -> dict[str, Any]:
        """Public durable grinder cooldown/recovery guard (SQLite sole authority).

        States:
        - ``ready``: no active grinder ownership and no active cooldown
        - ``cooldown``: confirmed stop rest interval still active
        - ``recovery_required``: non-terminal grinder/grinder_recovery workflow
        - ``unavailable``: storage error (fail closed)
        """

        base: dict[str, Any] = {
            "state": "ready",
            "workflow_id": None,
            "rest_seconds": GRINDER_REST_SECONDS,
            "remaining_s": None,
            "cooldown_until": None,
        }
        try:
            active = self.store.get_latest_workflow_for_kinds(
                sorted(GRINDER_WORKFLOW_KINDS),
                terminal=False,
            )
            if active is not None:
                base["state"] = "recovery_required"
                base["workflow_id"] = str(active.get("workflow_id") or "") or None
                # Surface known remaining cooldown only if recovery payload has it
                # (e.g. reserved interval); active motor ownership still fails closed.
                until = self._grinder_cooldown_until_from_workflow(active)
                if until is not None:
                    remaining = until - time.time()
                    base["cooldown_until"] = until
                    base["remaining_s"] = max(0.0, remaining)
                return base
            terminal = self.store.get_latest_workflow_for_kinds(
                sorted(GRINDER_WORKFLOW_KINDS),
                terminal=True,
            )
            if terminal is None:
                return base
            until = self._grinder_cooldown_until_from_workflow(terminal)
            wid = str(terminal.get("workflow_id") or "") or None
            base["workflow_id"] = wid
            if until is None:
                return base
            remaining = until - time.time()
            base["cooldown_until"] = until
            base["remaining_s"] = max(0.0, remaining)
            if remaining > 0:
                base["state"] = "cooldown"
            return base
        except StorageError:
            base["state"] = "unavailable"
            return base

    def _check_grinder_rest(self) -> None:
        """Block grinder start using durable SQLite guard (never JSON files)."""

        guard = self.grinder_guard()
        state = str(guard.get("state") or "unavailable")
        if state == "unavailable":
            raise BridgeError(
                "grinder guard unavailable (durable state unreadable); "
                "fail closed before BLE"
            )
        if state == "recovery_required":
            wid = guard.get("workflow_id")
            raise BridgeError(
                "a previous grinder session lacks a verified stop"
                + (f" (workflow_id={wid})" if wid else "")
                + "; cancel/recover before grinding"
            )
        if state == "cooldown":
            remaining = float(guard.get("remaining_s") or 0.0)
            raise BridgeError(
                f"grinder rest interval active; wait {int(remaining + 0.999)} more seconds"
            )

    def _require_idle_operation(self) -> None:
        if self.activity is not None:
            raise BridgeError(f"bridge is busy with {self.activity}:{self.phase}")
        if self.machine_state in ACTIVE_MACHINE_STATES:
            raise BridgeError(f"machine is not idle ({self.machine_state}); cancel first")

    def _require_idle_write_preflight(self) -> str:
        self._require_idle_operation()
        firmware = str(self.machine_info.get("firmware") or "")
        if not firmware:
            raise BridgeError(
                "machine-info handshake timed out: firmware version was not received; "
                "no recipe or start command was sent; retry when the machine is ready",
                category="machine_info_unavailable",
            )
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
        """Refuse new work while durable/in-memory workflow ownership is active.

        Legacy armed-state.json / tea-loaded-state.json are import-only and are
        never inspected here. Active durable workflow / in-memory activity is
        the sole gate.
        """

        if self.activity is not None:
            raise BridgeError(
                f"bridge owns {self.activity}:{self.phase}; recover/cancel first"
            )
        if self.active_workflow_id is not None:
            raise BridgeError(
                f"active durable workflow {self.active_workflow_id!r} still owns "
                "the bridge; recover/cancel first"
            )
        try:
            active_wf = self.store.get_active_workflow()
        except StorageError as exc:
            raise BridgeError(
                f"durable workflow state unreadable; refuse new work: {exc}",
                category="durable_state_unreadable",
            ) from exc
        if active_wf is not None:
            raise BridgeError(
                f"active durable workflow {active_wf.get('workflow_id')!r} "
                f"(state={active_wf.get('state')!r}); recover/cancel first"
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

    async def _read_initial_machine_info(self, newly_connected: bool) -> dict[str, Any]:
        # Some firmware reports 40521 only during the new session handshake.
        if newly_connected and self._machine_info_ready.is_set():
            return _public_machine_info(self.machine_info)
        return _public_machine_info(await self.client.read_machine_info())

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
            info = await self._read_initial_machine_info(newly)
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
        newly = await self._ensure_connected(params, scope="one-shot")
        try:
            self._require_idle_operation()
            info = await self._read_initial_machine_info(newly)
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
            newly = await self._ensure_connected(params, scope="one-shot")
            firmware = self._require_idle_write_preflight()
            before_info = await self._read_initial_machine_info(newly)
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
        newly = await self._ensure_connected(params, scope="one-shot")
        try:
            self._require_idle_operation()
            info = await self._read_initial_machine_info(newly)
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
            newly = await self._ensure_connected(params, scope="one-shot")
            firmware = self._require_idle_write_preflight()
            info = await self._read_initial_machine_info(newly)
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

    def _grinder_cooldown_fields(self, *, now: float | None = None) -> dict[str, Any]:
        """Fields written into durable terminal event / last_operation on confirmed STOP."""

        stopped_at = time.time() if now is None else float(now)
        return {
            "grinder_stopped_at": stopped_at,
            "grinder_cooldown_until": stopped_at + GRINDER_REST_SECONDS,
            "grinder_rest_seconds": GRINDER_REST_SECONDS,
        }

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
        # Durable cooldown/recovery gate before any BLE connect/write.
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
        # Durable non-terminal workflow is the sole recovery marker (no JSON).
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
            "rest_seconds": GRINDER_REST_SECONDS,
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
            self._set_workflow_state(
                "stop_unconfirmed",
                recovery={
                    "reason": "grinder_stop_unconfirmed",
                    "operation": operation,
                    "error": str(stop_exc),
                },
            )
            self._recovery_required = True
            self._recovery_detail = {
                "reason": "grinder_stop_unconfirmed",
                "operation": operation,
                "workflow_id": self.active_workflow_id,
                "error": str(stop_exc),
            }
            raise BridgeError(self.last_error) from stop_exc
        cooldown = self._grinder_cooldown_fields()
        self._finish_activity(
            f"{operation}_failed_stopped",
            release_reason="grinder_confirmed_stop",
            **cooldown,
        )
        if self.phase == "recovery_required" or self._recovery_required:
            # Confirmed STOP on machine but durable terminal failed: keep recovery.
            self.last_error = (
                f"grinder {operation} failed; STOP/QUIT confirmed but durable "
                "terminal commit failed; recovery_required; BLE withheld"
            )
            raise BridgeError(self.last_error) from cause
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

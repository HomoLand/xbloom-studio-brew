"""Workflow lifecycle, event log, and idempotency methods for StateStore."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ._storage_import_bridge import *  # noqa: F401,F403


class StateStoreWorkflowMixin:
    """Workflow, event pagination, and idempotency methods mixed into StateStore."""

    @staticmethod
    def _encode_recovery_json(
        recovery: Mapping[str, Any] | _ClearRecovery | None,
        existing_json: str | None,
    ) -> str | None:
        """Map preserve / clear / replace semantics for recovery_json."""

        if recovery is CLEAR_RECOVERY:
            return None
        if recovery is None:
            return existing_json
        return canonical_json(dict(recovery))

    # ------------------------------------------------------------------
    # Workflow primitives
    # ------------------------------------------------------------------

    def create_workflow(
        self,
        *,
        workflow_id: str | None = None,
        kind: str,
        state: str = "created",
        recipe_revision_id: str | None = None,
        snapshot: Mapping[str, Any] | None = None,
        source: str | None = None,
        owner: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        wid = workflow_id or f"wf_{uuid4().hex}"
        now = utc_now()
        snap_json = canonical_json(dict(snapshot)) if snapshot is not None else None
        snap_sha = sha256_text(snap_json) if snap_json is not None else None
        recovery_json = (
            canonical_json(dict(recovery)) if recovery is not None else None
        )
        meta_json = canonical_json(dict(metadata or {}))

        def _insert(active: sqlite3.Connection) -> dict[str, Any]:
            try:
                active.execute(
                    """
                    INSERT INTO workflows (
                        workflow_id, kind, state, recipe_revision_id, snapshot_json,
                        snapshot_sha256, source, owner, machine_phase, recovery_json,
                        created_at, updated_at, terminal_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        wid,
                        kind,
                        state,
                        recipe_revision_id,
                        snap_json,
                        snap_sha,
                        source,
                        owner,
                        machine_phase,
                        recovery_json,
                        now,
                        now,
                        meta_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"workflow integrity failure: {exc}"
                ) from exc
            return {
                "workflow_id": wid,
                "kind": kind,
                "state": state,
                "recipe_revision_id": recipe_revision_id,
                "snapshot_sha256": snap_sha,
                "source": source,
                "owner": owner,
                "machine_phase": machine_phase,
                "created_at": now,
                "updated_at": now,
            }

        if conn is not None:
            return _insert(conn)
        with self.transaction() as active:
            return _insert(active)

    def create_workflow_with_event(
        self,
        *,
        workflow_id: str | None = None,
        kind: str,
        state: str = "created",
        recipe_revision_id: str | None = None,
        snapshot: Mapping[str, Any] | None = None,
        source: str | None = None,
        owner: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        event_type: str = "created",
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically create a workflow row and its initial event.

        Either both succeed or neither is visible -- no orphan active rows.
        """

        with self.transaction() as conn:
            wf = self.create_workflow(
                workflow_id=workflow_id,
                kind=kind,
                state=state,
                recipe_revision_id=recipe_revision_id,
                snapshot=snapshot,
                source=source,
                owner=owner,
                machine_phase=machine_phase,
                recovery=recovery,
                metadata=metadata,
                conn=conn,
            )
            payload = dict(event_payload or {})
            if "kind" not in payload:
                payload["kind"] = kind
            if "state" not in payload:
                payload["state"] = state
            if "snapshot_sha256" not in payload and wf.get("snapshot_sha256"):
                payload["snapshot_sha256"] = wf["snapshot_sha256"]
            event = self.append_workflow_event_in_tx(
                conn, wf["workflow_id"], event_type, payload
            )
        return {**wf, "event": event}

    def transition_workflow(
        self,
        workflow_id: str,
        *,
        state: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = None,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
        event_type: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically update workflow row and optionally append an event."""

        with self.transaction() as conn:
            updated = self.update_workflow_in_tx(
                conn,
                workflow_id,
                state=state,
                machine_phase=machine_phase,
                recovery=recovery,
                terminal=terminal,
                metadata=metadata,
            )
            event: dict[str, Any] | None = None
            if event_type is not None:
                payload = dict(event_payload or {})
                if state is not None and "state" not in payload:
                    payload["state"] = state
                event = self.append_workflow_event_in_tx(
                    conn, workflow_id, event_type, payload
                )
        return {**updated, "event": event}

    def update_workflow(
        self,
        workflow_id: str,
        *,
        state: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = None,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            return self.update_workflow_in_tx(
                conn,
                workflow_id,
                state=state,
                machine_phase=machine_phase,
                recovery=recovery,
                terminal=terminal,
                metadata=metadata,
            )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        data = _row_to_dict(row) or {}
        if data.get("snapshot_json"):
            data["snapshot"] = json.loads(data.pop("snapshot_json"))
        else:
            data.pop("snapshot_json", None)
            data["snapshot"] = None
        if data.get("recovery_json"):
            data["recovery"] = json.loads(data.pop("recovery_json"))
        else:
            data.pop("recovery_json", None)
            data["recovery"] = None
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        return data

    def append_workflow_event(
        self,
        workflow_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        seq: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        payload_json = canonical_json(dict(payload or {}))
        with self.transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if exists is None:
                raise StorageError(f"unknown workflow_id {workflow_id!r}")
            if seq is None:
                max_row = conn.execute(
                    "SELECT MAX(seq) AS n FROM workflow_events WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                seq = int(max_row["n"] or 0) + 1
            conn.execute(
                """
                INSERT INTO workflow_events (
                    workflow_id, seq, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (workflow_id, seq, event_type, payload_json, now),
            )
            row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {
            "id": int(row_id),
            "workflow_id": workflow_id,
            "seq": seq,
            "event_type": event_type,
            "payload": dict(payload or {}),
            "created_at": now,
        }

    def list_workflow_events(
        self, workflow_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        rows = self._connect().execute(
            """
            SELECT id, workflow_id, seq, event_type, payload_json, created_at
            FROM workflow_events
            WHERE workflow_id = ? AND seq > ?
            ORDER BY seq
            LIMIT ?
            """,
            (workflow_id, int(since_seq), int(limit) if limit is not None and limit >= 0 else -1),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = _row_to_dict(row) or {}
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def put_idempotency(
        self,
        request_id: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        result: Mapping[str, Any] | None = None,
        status: str = "completed",
        expires_at: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        params_sha = content_sha256(dict(params or {}))
        now = utc_now()
        result_json = canonical_json(dict(result)) if result is not None else None
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["params_sha256"] != params_sha:
                    raise StorageError(
                        f"idempotency conflict for request_id {request_id!r}: "
                        "params hash mismatch"
                    )
                if existing["method"] != method:
                    raise StorageError(
                        f"idempotency conflict for request_id {request_id!r}: "
                        "method mismatch"
                    )
                cached = _row_to_dict(existing) or {}
                if cached.get("result_json"):
                    cached["result"] = json.loads(cached.pop("result_json"))
                else:
                    cached.pop("result_json", None)
                    cached["result"] = None
                cached["cached"] = True
                return cached
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency (
                        request_id, method, params_sha256, result_json, status,
                        created_at, expires_at, workflow_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        method,
                        params_sha,
                        result_json,
                        status,
                        now,
                        expires_at,
                        workflow_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"idempotency integrity failure: {exc}"
                ) from exc
        return {
            "request_id": request_id,
            "method": method,
            "params_sha256": params_sha,
            "result": dict(result) if result is not None else None,
            "status": status,
            "created_at": now,
            "expires_at": expires_at,
            "workflow_id": workflow_id,
            "cached": False,
        }

    def get_idempotency(self, request_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM idempotency WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return self._normalize_idempotency_row(row)

    @staticmethod
    def _normalize_idempotency_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = _row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
        if data.get("result_json"):
            data["result"] = json.loads(data.pop("result_json"))
        else:
            data.pop("result_json", None)
            data["result"] = None
        return data

    def reserve_idempotency(
        self,
        request_id: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        workflow_id: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve a request_id before a machine write.

        Returns one of:
        - ``status=pending``, ``reserved=True`` -- newly reserved; caller may write
        - ``status=completed``, ``cached=True`` -- exact duplicate; return result
        - ``status=pending``, ``cached=True``, ``recovery_required=True`` -- prior
          attempt may have written the machine; never reissue the action
        - raises ``StorageError`` on method/params/workflow conflict
        """

        if not request_id or not str(request_id).strip():
            raise StorageError("request_id is required")
        rid = str(request_id).strip()
        params_sha = content_sha256(dict(params or {}))
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if existing is not None:
                if existing["method"] != method:
                    raise StorageError(
                        f"idempotency conflict for request_id {rid!r}: method mismatch"
                    )
                if existing["params_sha256"] != params_sha:
                    raise StorageError(
                        f"idempotency conflict for request_id {rid!r}: "
                        "params hash mismatch"
                    )
                existing_wf = existing["workflow_id"]
                if (existing_wf or None) != (workflow_id or None):
                    raise StorageError(
                        f"idempotency conflict for request_id {rid!r}: "
                        "workflow_id mismatch"
                    )
                cached = self._normalize_idempotency_row(existing)
                cached["cached"] = True
                cached["reserved"] = False
                if cached["status"] == IDEM_COMPLETED:
                    return cached
                if cached["status"] == IDEM_FAILED:
                    # Failed attempts may be retried with the same identity only
                    # after an explicit new reservation -- treat as conflict-free
                    # re-reserve by updating back to pending without a machine
                    # result. Callers that completed a clear pre-BLE failure use
                    # fail_idempotency; re-reserve is allowed for those.
                    conn.execute(
                        """
                        UPDATE idempotency SET
                            status = ?, result_json = NULL, created_at = ?,
                            expires_at = COALESCE(?, expires_at)
                        WHERE request_id = ?
                        """,
                        (IDEM_PENDING, now, expires_at, rid),
                    )
                    return {
                        "request_id": rid,
                        "method": method,
                        "params_sha256": params_sha,
                        "result": None,
                        "status": IDEM_PENDING,
                        "created_at": now,
                        "expires_at": expires_at or cached.get("expires_at"),
                        "workflow_id": workflow_id,
                        "cached": False,
                        "reserved": True,
                        "rereserved": True,
                    }
                # Pending: never reissue machine action.
                cached["recovery_required"] = True
                return cached
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency (
                        request_id, method, params_sha256, result_json, status,
                        created_at, expires_at, workflow_id
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        method,
                        params_sha,
                        IDEM_PENDING,
                        now,
                        expires_at,
                        workflow_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"idempotency integrity failure: {exc}"
                ) from exc
        return {
            "request_id": rid,
            "method": method,
            "params_sha256": params_sha,
            "result": None,
            "status": IDEM_PENDING,
            "created_at": now,
            "expires_at": expires_at,
            "workflow_id": workflow_id,
            "cached": False,
            "reserved": True,
        }

    def complete_idempotency(
        self,
        request_id: str,
        result: Mapping[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Mark a reserved request completed with a durable result."""

        rid = str(request_id).strip()
        result_json = canonical_json(dict(result))
        now = utc_now()

        def _complete(active: sqlite3.Connection) -> dict[str, Any]:
            row = active.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown request_id {rid!r}")
            if row["status"] == IDEM_COMPLETED and row["result_json"] is not None:
                cached = self._normalize_idempotency_row(row)
                cached["cached"] = True
                return cached
            active.execute(
                """
                UPDATE idempotency SET status = ?, result_json = ?
                WHERE request_id = ?
                """,
                (IDEM_COMPLETED, result_json, rid),
            )
            return {
                "request_id": rid,
                "method": row["method"],
                "params_sha256": row["params_sha256"],
                "result": dict(result),
                "status": IDEM_COMPLETED,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "workflow_id": row["workflow_id"],
                "cached": False,
                "completed_at": now,
            }

        if conn is not None:
            return _complete(conn)
        with self.transaction() as active:
            return _complete(active)

    def fail_idempotency(
        self,
        request_id: str,
        error: Mapping[str, Any] | str,
        *,
        conn: sqlite3.Connection | None = None,
        keep_pending: bool = False,
    ) -> dict[str, Any]:
        """Record a failed or recovery-bound idempotency outcome.

        When ``keep_pending`` is True (uncertain machine write), status stays
        ``pending`` so a retry surfaces recovery_required rather than reissuing.
        """

        rid = str(request_id).strip()
        if isinstance(error, str):
            payload: dict[str, Any] = {"error": error}
        else:
            payload = dict(error)
        result_json = canonical_json(payload)

        def _fail(active: sqlite3.Connection) -> dict[str, Any]:
            row = active.execute(
                "SELECT * FROM idempotency WHERE request_id = ?",
                (rid,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown request_id {rid!r}")
            status = IDEM_PENDING if keep_pending else IDEM_FAILED
            active.execute(
                """
                UPDATE idempotency SET status = ?, result_json = ?
                WHERE request_id = ?
                """,
                (status, result_json, rid),
            )
            return {
                "request_id": rid,
                "method": row["method"],
                "params_sha256": row["params_sha256"],
                "result": payload,
                "status": status,
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "workflow_id": row["workflow_id"],
                "cached": False,
                "recovery_required": keep_pending,
            }

        if conn is not None:
            return _fail(conn)
        with self.transaction() as active:
            return _fail(active)

    # ------------------------------------------------------------------
    # Active / latest workflow queries
    # ------------------------------------------------------------------

    def get_active_workflow(self) -> dict[str, Any] | None:
        """Return the newest non-terminal workflow, or None."""

        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT * FROM workflows
            WHERE terminal_at IS NULL
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self.get_workflow(str(row["workflow_id"]))

    def get_latest_workflow(self) -> dict[str, Any] | None:
        """Return the most recently updated workflow (terminal or not)."""

        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT workflow_id FROM workflows
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return self.get_workflow(str(row["workflow_id"]))

    def get_latest_workflow_for_kinds(
        self,
        kinds: Sequence[str],
        *,
        terminal: bool | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest workflow among a validated set of kinds.

        Ordering is deterministic: ``updated_at`` DESC, ``created_at`` DESC,
        then SQLite ``rowid`` DESC as a final insertion-identity tie-breaker.
        ``utc_now()`` is second-resolution, so two terminal commits in the same
        second would otherwise be non-deterministic without ``rowid``.

        ``terminal`` filters:
        - ``True``: only rows with ``terminal_at`` set
        - ``False``: only non-terminal rows
        - ``None``: either
        """

        if not kinds:
            raise StorageError("kinds must be a non-empty sequence")
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in kinds:
            if not isinstance(raw, str) or not raw.strip():
                raise StorageError(f"invalid workflow kind {raw!r}")
            kind = raw.strip()
            if kind not in KNOWN_WORKFLOW_KINDS:
                raise StorageError(f"unknown workflow kind {kind!r}")
            if kind not in seen:
                seen.add(kind)
                normalized.append(kind)
        placeholders = ", ".join("?" for _ in normalized)
        if terminal is True:
            terminal_clause = "AND terminal_at IS NOT NULL"
        elif terminal is False:
            terminal_clause = "AND terminal_at IS NULL"
        else:
            terminal_clause = ""
        self.ensure_schema()
        row = self._connect().execute(
            f"""
            SELECT workflow_id FROM workflows
            WHERE kind IN ({placeholders}) {terminal_clause}
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            tuple(normalized),
        ).fetchone()
        if row is None:
            return None
        return self.get_workflow(str(row["workflow_id"]))

    def list_active_workflows(self) -> list[dict[str, Any]]:
        """All non-terminal workflows, newest first."""

        self.ensure_schema()
        rows = self._connect().execute(
            """
            SELECT workflow_id FROM workflows
            WHERE terminal_at IS NULL
            ORDER BY updated_at DESC, created_at DESC, rowid DESC
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            wf = self.get_workflow(str(row["workflow_id"]))
            if wf is not None:
                result.append(wf)
        return result

    def workflow_summary(self, workflow_id: str | None = None) -> dict[str, Any] | None:
        """Public durable summary for status() / clients."""

        wf = (
            self.get_workflow(workflow_id)
            if workflow_id is not None
            else self.get_active_workflow() or self.get_latest_workflow()
        )
        if wf is None:
            return None
        return {
            "workflow_id": wf["workflow_id"],
            "kind": wf.get("kind"),
            "state": wf.get("state"),
            "source": wf.get("source"),
            "owner": wf.get("owner"),
            "snapshot_sha256": wf.get("snapshot_sha256"),
            "recipe_revision_id": wf.get("recipe_revision_id"),
            "machine_phase": wf.get("machine_phase"),
            "recovery": wf.get("recovery"),
            "created_at": wf.get("created_at"),
            "updated_at": wf.get("updated_at"),
            "terminal_at": wf.get("terminal_at"),
            "metadata": wf.get("metadata") or {},
        }

    def list_workflow_events_page(
        self,
        workflow_id: str,
        *,
        since_seq: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Durable event cursor page with explicit gap contract.

        Durable rows are dense (seq increases by 1). ``gap_detected`` is True
        only when the caller skips past known history (since > max durable seq
        for a known workflow) or the workflow is unknown. Artificial gaps are
        never introduced by append.
        """

        self.ensure_schema()
        exists = self._connect().execute(
            "SELECT 1 FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if exists is None:
            return {
                "workflow_id": workflow_id,
                "events": [],
                "next_since": int(since_seq),
                "gap_detected": True,
                "gap_reason": "unknown_workflow",
                "max_seq": 0,
            }
        max_row = self._connect().execute(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM workflow_events WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        max_seq = int(max_row["n"] or 0)
        since = max(0, int(since_seq))
        if since > max_seq:
            return {
                "workflow_id": workflow_id,
                "events": [],
                "next_since": max_seq,
                "gap_detected": True,
                "gap_reason": "since_beyond_history",
                "max_seq": max_seq,
            }
        # Bound the SQLite read before decoding payloads; historical workflows can have millions of events.
        events = self.list_workflow_events(workflow_id, since_seq=since, limit=limit)
        next_since = int(events[-1]["seq"]) if events else since
        # Dense sequence: if first returned seq is not since+1 when events exist
        # and since < max, that would be a real gap -- should not happen.
        gap_detected = False
        gap_reason: str | None = None
        if events and since > 0:
            expected = since + 1
            if int(events[0]["seq"]) != expected:
                gap_detected = True
                gap_reason = "sequence_hole"
        return {
            "workflow_id": workflow_id,
            "events": events,
            "next_since": next_since if events else since,
            "gap_detected": gap_detected,
            "gap_reason": gap_reason,
            "max_seq": max_seq,
        }

    def commit_workflow_terminal(
        self,
        workflow_id: str,
        *,
        state: str,
        event_type: str = "terminal",
        event_payload: Mapping[str, Any] | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = CLEAR_RECOVERY,
        machine_phase: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        idempotency_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically finalize workflow, workflow event, history, and idempotency.

        One ``BEGIN IMMEDIATE`` transaction covers:
        - workflow terminal row update
        - final workflow_events row
        - exactly one final history_events row (dedupe ``workflow:<id>:terminal``)
        - optional idempotency completion

        Any history insert fault rolls the whole commit back so callers must not
        claim BLE release succeeded. Terminal re-entry is idempotent for the
        history row (same dedupe key) while preserving first ``terminal_at``.

        Terminal commits default to clearing ``recovery_json`` (``CLEAR_RECOVERY``).
        Pass ``recovery=None`` to preserve an existing recovery payload, or a
        mapping to store a final recovery note.
        """

        now = utc_now()
        payload = dict(event_payload or {})
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown workflow_id {workflow_id!r}")

            # Terminal re-entry: preserve first durable terminal state/event/
            # history. Identical state returns without new workflow_events;
            # conflicting state must fail rather than rewrite the workflow
            # while leaving the first history row in place.
            if row["terminal_at"] is not None:
                if str(row["state"]) != str(state):
                    raise StorageConflictError(
                        f"workflow {workflow_id!r} already terminal in state "
                        f"{row['state']!r}; cannot re-enter with {state!r}"
                    )
                hist_key = workflow_terminal_history_dedupe_key(workflow_id)
                hist_row = conn.execute(
                    "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
                    (hist_key,),
                ).fetchone()
                history_event = (
                    self._history_row_to_public(hist_row)
                    if hist_row is not None
                    else None
                )
                # If history is missing (pre-cutover terminal), append once
                # under the terminal dedupe key without rewriting workflow.
                if history_event is None:
                    row_dict = _row_to_dict(row) or {}
                    history_payload = history_event_from_workflow_terminal(
                        row_dict,
                        state=str(row["state"]),
                        event_payload=payload,
                        terminal_at=str(row["terminal_at"]),
                    )
                    history_event, _ = self._append_history_event_in_tx(
                        conn,
                        history_payload,
                        dedupe_key=hist_key,
                        created_at=now,
                    )
                last_ev = conn.execute(
                    """
                    SELECT id, seq, event_type, payload_json, created_at
                    FROM workflow_events
                    WHERE workflow_id = ?
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (workflow_id,),
                ).fetchone()
                event_out: dict[str, Any]
                if last_ev is not None:
                    try:
                        last_payload = json.loads(last_ev["payload_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        last_payload = {}
                    event_out = {
                        "id": int(last_ev["id"]),
                        "workflow_id": workflow_id,
                        "seq": int(last_ev["seq"]),
                        "event_type": last_ev["event_type"],
                        "payload": last_payload if isinstance(last_payload, dict) else {},
                        "created_at": last_ev["created_at"],
                    }
                else:
                    event_out = {
                        "id": None,
                        "workflow_id": workflow_id,
                        "seq": 0,
                        "event_type": event_type,
                        "payload": payload,
                        "created_at": row["terminal_at"],
                    }
                idem: dict[str, Any] | None = None
                if request_id is not None and idempotency_result is not None:
                    idem = self.complete_idempotency(
                        request_id, idempotency_result, conn=conn
                    )
                elif request_id is not None:
                    idem = self.complete_idempotency(
                        request_id,
                        {
                            "status": state,
                            "workflow_id": workflow_id,
                            "terminal": True,
                            **payload,
                        },
                        conn=conn,
                    )
                return {
                    "workflow_id": workflow_id,
                    "state": str(row["state"]),
                    "terminal_at": row["terminal_at"],
                    "updated_at": row["updated_at"],
                    "event": event_out,
                    "history_event": history_event,
                    "idempotency": idem,
                    "reentered": True,
                }

            new_phase = (
                machine_phase if machine_phase is not None else row["machine_phase"]
            )
            new_recovery = self._encode_recovery_json(
                recovery, row["recovery_json"]
            )
            new_meta = (
                canonical_json(dict(metadata))
                if metadata is not None
                else row["metadata_json"]
            )
            terminal_at = now
            conn.execute(
                """
                UPDATE workflows SET
                    state = ?, machine_phase = ?, recovery_json = ?,
                    updated_at = ?, terminal_at = ?, metadata_json = ?
                WHERE workflow_id = ?
                """,
                (
                    state,
                    new_phase,
                    new_recovery,
                    now,
                    terminal_at,
                    new_meta,
                    workflow_id,
                ),
            )
            max_row = conn.execute(
                "SELECT MAX(seq) AS n FROM workflow_events WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            seq = int(max_row["n"] or 0) + 1
            payload_json = canonical_json(payload)
            conn.execute(
                """
                INSERT INTO workflow_events (
                    workflow_id, seq, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (workflow_id, seq, event_type, payload_json, now),
            )
            event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            # Build history from the immutable snapshot row + terminal payload.
            row_dict = _row_to_dict(row) or {}
            if metadata is not None:
                row_dict["metadata"] = dict(metadata)
            history_payload = history_event_from_workflow_terminal(
                row_dict,
                state=state,
                event_payload=payload,
                terminal_at=terminal_at,
            )
            history_event, _ = self._append_history_event_in_tx(
                conn,
                history_payload,
                dedupe_key=workflow_terminal_history_dedupe_key(workflow_id),
                created_at=now,
            )
            idem = None
            if request_id is not None and idempotency_result is not None:
                idem = self.complete_idempotency(
                    request_id, idempotency_result, conn=conn
                )
            elif request_id is not None:
                # Complete with terminal payload when no explicit result given.
                idem = self.complete_idempotency(
                    request_id,
                    {
                        "status": state,
                        "workflow_id": workflow_id,
                        "terminal": True,
                        **payload,
                    },
                    conn=conn,
                )
        return {
            "workflow_id": workflow_id,
            "state": state,
            "terminal_at": terminal_at,
            "updated_at": now,
            "event": {
                "id": event_id,
                "workflow_id": workflow_id,
                "seq": seq,
                "event_type": event_type,
                "payload": payload,
                "created_at": now,
            },
            "history_event": history_event,
            "idempotency": idem,
        }

    def append_workflow_event_in_tx(
        self,
        conn: sqlite3.Connection,
        workflow_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an event using an existing transaction connection."""

        now = utc_now()
        payload_json = canonical_json(dict(payload or {}))
        exists = conn.execute(
            "SELECT 1 FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if exists is None:
            raise StorageError(f"unknown workflow_id {workflow_id!r}")
        max_row = conn.execute(
            "SELECT MAX(seq) AS n FROM workflow_events WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        seq = int(max_row["n"] or 0) + 1
        conn.execute(
            """
            INSERT INTO workflow_events (
                workflow_id, seq, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (workflow_id, seq, event_type, payload_json, now),
        )
        row_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        return {
            "id": row_id,
            "workflow_id": workflow_id,
            "seq": seq,
            "event_type": event_type,
            "payload": dict(payload or {}),
            "created_at": now,
        }

    def update_workflow_in_tx(
        self,
        conn: sqlite3.Connection,
        workflow_id: str,
        *,
        state: str | None = None,
        machine_phase: str | None = None,
        recovery: Mapping[str, Any] | _ClearRecovery | None = None,
        terminal: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update workflow row inside an existing transaction."""

        now = utc_now()
        row = conn.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"unknown workflow_id {workflow_id!r}")
        new_state = state if state is not None else row["state"]
        new_phase = (
            machine_phase if machine_phase is not None else row["machine_phase"]
        )
        new_recovery = self._encode_recovery_json(recovery, row["recovery_json"])
        new_meta = (
            canonical_json(dict(metadata))
            if metadata is not None
            else row["metadata_json"]
        )
        terminal_at = now if terminal else row["terminal_at"]
        conn.execute(
            """
            UPDATE workflows SET
                state = ?, machine_phase = ?, recovery_json = ?,
                updated_at = ?, terminal_at = ?, metadata_json = ?
            WHERE workflow_id = ?
            """,
            (
                new_state,
                new_phase,
                new_recovery,
                now,
                terminal_at,
                new_meta,
                workflow_id,
            ),
        )
        return {
            "workflow_id": workflow_id,
            "state": new_state,
            "machine_phase": new_phase,
            "updated_at": now,
            "terminal_at": terminal_at,
        }

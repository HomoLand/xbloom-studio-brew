"""History journal, beans, preferences, integrity, backup, and legacy import methods."""

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


class StateStoreJournalMixin:
    """Append-only history journal, beans, preferences, and maintenance methods."""

    # ------------------------------------------------------------------
    # History journal (append-only; authoritative runtime store)
    # ------------------------------------------------------------------

    def _prepare_history_payload(
        self,
        event: Mapping[str, Any],
        *,
        require_outcome: bool = True,
    ) -> dict[str, Any]:
        """Validate and normalise a public history event payload."""

        payload = public_history_event(event)
        if "event_id" not in payload or not str(payload.get("event_id") or "").strip():
            payload["event_id"] = new_history_event_id()
        else:
            payload["event_id"] = str(payload["event_id"]).strip()
        if "recorded_at" not in payload or not payload.get("recorded_at"):
            payload["recorded_at"] = utc_now()
        if "schema_version" not in payload:
            payload["schema_version"] = 1
        outcome = str(payload.get("outcome") or "").strip()
        if require_outcome and outcome not in HISTORY_VALID_OUTCOMES:
            raise StorageError(
                f"history outcome must be one of {sorted(HISTORY_VALID_OUTCOMES)}; "
                f"got {outcome!r}"
            )
        if outcome:
            payload["outcome"] = outcome
        source = str(payload.get("source") or HISTORY_SOURCE_LOCAL).strip() or (
            HISTORY_SOURCE_LOCAL
        )
        payload["source"] = source
        # Bound free-text note if present.
        if "note" in payload and payload["note"] is not None:
            cleaned = _history_clean_text(
                payload["note"], field="note", max_chars=MAX_HISTORY_NOTE_CHARS
            )
            if cleaned is None:
                payload.pop("note", None)
            else:
                payload["note"] = cleaned
        return payload

    def _history_row_to_public(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            payload_json = row["payload_json"]
        else:
            payload_json = row.get("payload_json")
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StorageError(f"corrupt history payload_json: {exc}") from exc
        if not isinstance(payload, dict):
            raise StorageError("history payload_json must be a JSON object")
        return public_history_event(payload)

    def _history_payloads_conflict(
        self,
        existing: Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        original_event: Mapping[str, Any],
    ) -> bool:
        """True when a retry carries a materially different public payload.

        Auto-filled fields (``recorded_at``, ``schema_version``) are aligned from
        the existing row when the caller did not supply them, so pure retries
        remain idempotent.
        """

        left = public_history_event(existing)
        right = public_history_event(candidate)
        # Align store-assigned fields the caller did not supply so pure retries
        # and concurrent same-key inserts (e.g. app remote) stay idempotent.
        if not str(original_event.get("event_id") or "").strip():
            right["event_id"] = left.get("event_id")
        if not original_event.get("recorded_at"):
            right["recorded_at"] = left.get("recorded_at")
        if "schema_version" not in original_event:
            right["schema_version"] = left.get("schema_version", 1)
        return canonical_json(left) != canonical_json(right)

    def _assert_history_compatible(
        self,
        existing_row: sqlite3.Row | Mapping[str, Any],
        candidate: Mapping[str, Any],
        *,
        original_event: Mapping[str, Any],
        dedupe_key: str,
    ) -> dict[str, Any]:
        existing_public = self._history_row_to_public(existing_row)
        if self._history_payloads_conflict(
            existing_public, candidate, original_event=original_event
        ):
            raise StorageConflictError(
                f"history dedupe conflict for {dedupe_key!r}: "
                "retry payload differs from the durable event"
            )
        return existing_public

    def _append_history_event_in_tx(
        self,
        conn: sqlite3.Connection,
        event: Mapping[str, Any],
        *,
        dedupe_key: str | None = None,
        created_at: str | None = None,
        require_outcome: bool = True,
    ) -> tuple[dict[str, Any], bool]:
        """Insert one history row inside an open transaction.

        Returns ``(public_event, created)``. Identical retries are idempotent;
        a materially different public payload for the same dedupe key raises
        :class:`StorageConflictError`.
        """

        original_event = dict(event)
        payload = self._prepare_history_payload(
            event, require_outcome=require_outcome
        )
        key = dedupe_key or history_event_dedupe_key(str(payload["event_id"]))
        now = created_at or utc_now()
        existing = conn.execute(
            "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            public = self._assert_history_compatible(
                existing,
                payload,
                original_event=original_event,
                dedupe_key=key,
            )
            return public, False

        remote_id = payload.get("remote_table_id")
        remote_s = None if remote_id is None else str(remote_id)
        try:
            conn.execute(
                """
                INSERT INTO history_events (
                    dedupe_key, event_id, recorded_at, outcome, source, event_kind,
                    recipe_sha256, recipe_name, recipe_path, machine, serving_kind,
                    machine_program, note, related_event_id, remote_table_id,
                    workflow_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    str(payload["event_id"]),
                    payload.get("recorded_at"),
                    payload.get("outcome"),
                    payload.get("source"),
                    payload.get("event_kind"),
                    payload.get("recipe_sha256"),
                    payload.get("recipe_name"),
                    payload.get("recipe_path"),
                    payload.get("machine"),
                    payload.get("serving_kind"),
                    payload.get("machine_program"),
                    payload.get("note"),
                    payload.get("related_event_id"),
                    remote_s,
                    payload.get("workflow_id"),
                    canonical_json(payload),
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Concurrent insert of the same dedupe key: return winner if compatible.
            raced = conn.execute(
                "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
                (key,),
            ).fetchone()
            if raced is not None:
                public = self._assert_history_compatible(
                    raced,
                    payload,
                    original_event=original_event,
                    dedupe_key=key,
                )
                return public, False
            raise StorageError(f"history insert integrity failure: {exc}") from exc
        return public_history_event(payload), True

    def append_history_event(
        self,
        event: Mapping[str, Any],
        *,
        dedupe_key: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Append one public history event. Idempotent on ``dedupe_key`` / event_id."""

        if conn is not None:
            public, _ = self._append_history_event_in_tx(
                conn, event, dedupe_key=dedupe_key
            )
            return public
        with self.transaction() as active:
            public, _ = self._append_history_event_in_tx(
                active, event, dedupe_key=dedupe_key
            )
            return public

    # ------------------------------------------------------------------
    # Beans + preferences (Phase E)
    # ------------------------------------------------------------------

    def _bean_row_to_public(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = _row_to_dict(row) if isinstance(row, sqlite3.Row) else dict(row)
        meta_raw = data.pop("metadata_json", "{}")
        try:
            metadata = json.loads(meta_raw or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        data["metadata"] = metadata
        return data

    def list_beans(self, *, query: str | None = None) -> list[dict[str, Any]]:
        """List beans newest-updated first; optional case-insensitive name/origin query."""

        self.ensure_schema()
        sql = "SELECT * FROM beans"
        params: list[Any] = []
        if query and query.strip():
            like = f"%{query.strip()}%"
            sql += (
                " WHERE name LIKE ? COLLATE NOCASE"
                " OR IFNULL(origin, '') LIKE ? COLLATE NOCASE"
                " OR IFNULL(flavor_notes, '') LIKE ? COLLATE NOCASE"
            )
            params.extend([like, like, like])
        sql += " ORDER BY updated_at DESC"
        rows = self._connect().execute(sql, params).fetchall()
        return [self._bean_row_to_public(row) for row in rows]

    def get_bean(self, bean_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM beans WHERE bean_id = ?",
            (bean_id,),
        ).fetchone()
        return self._bean_row_to_public(row) if row is not None else None

    def upsert_bean(
        self,
        *,
        name: str,
        bean_id: str | None = None,
        origin: str | None = None,
        process: str | None = None,
        roast_level: str | None = None,
        altitude: str | None = None,
        flavor_notes: str | None = None,
        roast_date: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a bean library entry."""

        cleaned = (name or "").strip()
        if not cleaned:
            raise StorageError("bean name is required")
        now = utc_now()
        bid = (bean_id or "").strip() or f"bean_{uuid4().hex}"
        meta_json = json.dumps(dict(metadata or {}), sort_keys=True, ensure_ascii=False)
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT bean_id FROM beans WHERE bean_id = ?",
                (bid,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO beans (
                        bean_id, name, origin, process, roast_level, altitude,
                        flavor_notes, roast_date, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bid,
                        cleaned,
                        origin,
                        process,
                        roast_level,
                        altitude,
                        flavor_notes,
                        roast_date,
                        meta_json,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE beans SET
                        name = ?, origin = ?, process = ?, roast_level = ?,
                        altitude = ?, flavor_notes = ?, roast_date = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE bean_id = ?
                    """,
                    (
                        cleaned,
                        origin,
                        process,
                        roast_level,
                        altitude,
                        flavor_notes,
                        roast_date,
                        meta_json,
                        now,
                        bid,
                    ),
                )
        out = self.get_bean(bid)
        assert out is not None
        return out

    def delete_bean(self, bean_id: str) -> bool:
        self.ensure_schema()
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM beans WHERE bean_id = ?", (bean_id,))
            return cur.rowcount > 0

    def get_preference(self, key: str) -> Any | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT value_json FROM preferences WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return None

    def list_preferences(self) -> dict[str, Any]:
        self.ensure_schema()
        rows = self._connect().execute(
            "SELECT key, value_json FROM preferences ORDER BY key ASC"
        ).fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[str(row["key"])] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                out[str(row["key"])] = None
        return out

    def set_preference(self, key: str, value: Any) -> dict[str, Any]:
        cleaned = (key or "").strip()
        if not cleaned:
            raise StorageError("preference key is required")
        now = utc_now()
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO preferences (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (cleaned, payload, now),
            )
        return {"key": cleaned, "value": value, "updated_at": now}

    def delete_preference(self, key: str) -> bool:
        self.ensure_schema()
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM preferences WHERE key = ?", (key,))
            return cur.rowcount > 0

    def load_history_events(self) -> list[dict[str, Any]]:
        """Return all history events in append order (oldest first)."""

        self.ensure_schema()
        rows = self._connect().execute(
            "SELECT payload_json FROM history_events ORDER BY id ASC"
        ).fetchall()
        return [self._history_row_to_public(row) for row in rows]

    def list_history_events(
        self,
        *,
        limit: int = DEFAULT_HISTORY_LIST_LIMIT,
        source: str | None = None,
        outcome: str | None = None,
        query: str | None = None,
        recipe_sha256: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filter history newest-first with the public list contract.

        Free-text ``query`` is applied as SQL ``LIKE`` over public text columns
        across the full matching journal (no newest-window cutoff), then limited.
        """

        if not 1 <= int(limit) <= MAX_HISTORY_LIST_LIMIT:
            raise StorageError(
                f"history list limit must be 1-{MAX_HISTORY_LIST_LIMIT}"
            )
        needle = (query or "").strip()
        wanted_source = (source or "").strip() or None
        wanted_outcome = (outcome or "").strip() or None
        wanted_sha = (recipe_sha256 or "").strip() or None

        clauses: list[str] = []
        params: list[Any] = []
        if wanted_source is not None:
            clauses.append("source = ?")
            params.append(wanted_source)
        if wanted_outcome is not None:
            clauses.append("outcome = ?")
            params.append(wanted_outcome)
        if wanted_sha is not None:
            clauses.append("recipe_sha256 = ?")
            params.append(wanted_sha)
        if needle:
            # Escape LIKE metacharacters so the needle is a literal substring.
            like_raw = (
                needle.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like = f"%{like_raw}%"
            text_cols = (
                "event_id",
                "recipe_name",
                "recipe_path",
                "machine",
                "note",
                "serving_kind",
                "machine_program",
                "outcome",
                "source",
            )
            like_parts = [
                f"LOWER(IFNULL({col}, '')) LIKE LOWER(?) ESCAPE '\\'"
                for col in text_cols
            ]
            clauses.append(f"({' OR '.join(like_parts)})")
            params.extend([like] * len(text_cols))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        self.ensure_schema()
        rows = self._connect().execute(
            f"""
            SELECT payload_json FROM history_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        return [self._history_row_to_public(row) for row in rows]

    def history_summary(self) -> dict[str, Any]:
        """Aggregate counts; path/source declare SQLite state.db as authoritative."""

        events = self.load_history_events()
        by_outcome: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for event in events:
            outcome = str(event.get("outcome") or "unknown")
            source = str(event.get("source") or "unknown")
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
        return {
            "path": str(self.db_path),
            "db_path": str(self.db_path),
            "state_root": str(self.state_root),
            "source": "state.db",
            "authoritative": "sqlite",
            "exists": self.db_path.is_file(),
            "total": len(events),
            "by_outcome": by_outcome,
            "by_source": by_source,
            "latest_recorded_at": events[-1].get("recorded_at") if events else None,
        }

    def add_history_note(self, event_id: str, note: str) -> dict[str, Any]:
        """Append a linked tasting note without rewriting earlier rows."""

        cleaned = _history_clean_text(
            note, field="note", max_chars=MAX_HISTORY_NOTE_CHARS
        )
        if not cleaned:
            raise StorageError("note must be non-empty")
        target_id = str(event_id or "").strip()
        if not target_id:
            raise StorageError("event_id is required")
        matches = [
            event
            for event in self.load_history_events()
            if event.get("event_id") == target_id
        ]
        if not matches:
            raise StorageError(f"history event {target_id!r} was not found")
        target = matches[-1]
        return self.append_history_event(
            {
                "outcome": target.get("outcome") or "imported",
                "source": HISTORY_SOURCE_LOCAL,
                "event_kind": "note",
                "related_event_id": target_id,
                "recipe_name": target.get("recipe_name"),
                "recipe_path": target.get("recipe_path"),
                "recipe_sha256": target.get("recipe_sha256"),
                "machine": target.get("machine"),
                "serving_kind": target.get("serving_kind"),
                "machine_program": target.get("machine_program"),
                "note": cleaned,
            }
        )

    def import_app_history_records(
        self,
        records: Sequence[Mapping[str, Any]],
        *,
        region: str | None = None,
    ) -> dict[str, Any]:
        """Import App brew-record dicts; skip known remote_table_id values.

        Concurrent losers that race on the same remote dedupe key count as
        ``skipped_existing`` (one durable row only), not as ``imported``.
        """

        existing = self.load_history_events()
        known_remote_ids = {
            str(event.get("remote_table_id"))
            for event in existing
            if event.get("source") == HISTORY_SOURCE_APP
            and event.get("remote_table_id") is not None
        }
        imported = 0
        skipped = 0
        written: list[dict[str, Any]] = []
        with self.transaction() as conn:
            for raw in records:
                remote_id = raw.get("remote_table_id")
                if remote_id is not None and str(remote_id) in known_remote_ids:
                    skipped += 1
                    continue
                event = {
                    "event_kind": "app_brew_record",
                    "outcome": "imported",
                    "source": HISTORY_SOURCE_APP,
                    "region": region,
                    "remote_table_id": remote_id,
                    "recipe_name": raw.get("recipe_name"),
                    "serving_kind": raw.get("serving_kind"),
                    "machine_program": raw.get("machine_program"),
                    "cup_type": raw.get("cup_type"),
                    "dose_g": raw.get("dose_g"),
                    "brew_time_s": raw.get("brew_time_s"),
                    "create_time_stamp": raw.get("create_time_stamp"),
                    # Leave recorded_at unset when absent so prepare() fills it
                    # and concurrent same-remote races can align for comparison.
                    "recorded_at": raw.get("recorded_at"),
                    "has_line_chart": bool(raw.get("has_line_chart")),
                    "is_pod": raw.get("is_pod"),
                    "machine_id": raw.get("machine_id"),
                    "member_used_recipes_id": raw.get("member_used_recipes_id"),
                    "group_name": raw.get("group_name"),
                    "recipe_sha256": raw.get("recipe_sha256"),
                }
                event = {
                    key: value
                    for key, value in event.items()
                    if value not in (None, "", [], {})
                }
                # Prefer stable remote dedupe when a table id is present.
                dedupe: str | None = None
                if remote_id is not None:
                    dedupe = f"app:remote:{remote_id}"
                    # Re-check under the write transaction so concurrent losers
                    # observe the winner before counting as imported.
                    present = conn.execute(
                        "SELECT 1 FROM history_events WHERE dedupe_key = ?",
                        (dedupe,),
                    ).fetchone()
                    if present is not None:
                        known_remote_ids.add(str(remote_id))
                        skipped += 1
                        continue
                written_event, created = self._append_history_event_in_tx(
                    conn, event, dedupe_key=dedupe
                )
                if not created:
                    # Race: peer already inserted the same remote key.
                    if remote_id is not None:
                        known_remote_ids.add(str(remote_id))
                    skipped += 1
                    continue
                written.append(written_event)
                if remote_id is not None:
                    known_remote_ids.add(str(remote_id))
                imported += 1
        return {
            "imported": imported,
            "skipped_existing": skipped,
            "written_event_ids": [item["event_id"] for item in written],
        }

    def count_history_events(self) -> int:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT COUNT(*) AS n FROM history_events"
        ).fetchone()
        return int(row["n"])

    def get_history_event_by_dedupe_key(
        self, dedupe_key: str
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT payload_json FROM history_events WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if row is None:
            return None
        return self._history_row_to_public(row)

    # ------------------------------------------------------------------
    # Integrity and backup
    # ------------------------------------------------------------------

    def integrity_check(self) -> dict[str, Any]:
        self.ensure_schema()
        conn = self._connect()
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        messages = [str(row[0]) for row in rows]
        ok = messages == ["ok"]
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        fk_issues = [
            {"table": r[0], "rowid": r[1], "parent": r[2], "fkid": r[3]}
            for r in foreign
        ]
        return {
            "ok": ok and not fk_issues,
            "integrity_check": messages,
            "foreign_key_issues": fk_issues,
            "schema_version": self.schema_version(),
            "db_path": str(self.db_path),
        }

    def backup(self, destination: Path | str | None = None) -> Path:
        """Online backup via SQLite backup API. Returns destination path."""

        self.ensure_schema()
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            dest_dir = self.state_root / DEFAULT_BACKUP_DIRNAME
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"state-{stamp}.db"
            # Avoid collisions if two backups start in the same microsecond.
            if dest.exists():
                dest = dest_dir / f"state-{stamp}-{uuid4().hex[:8]}.db"
        else:
            dest = Path(destination)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise StorageError(f"backup destination already exists: {dest}")
        source = self._connect()
        dest_conn = sqlite3.connect(str(dest))
        try:
            source.backup(dest_conn)
        finally:
            dest_conn.close()
        return dest

    def journal_mode(self) -> str:
        self.ensure_schema()
        row = self._connect().execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    # ------------------------------------------------------------------
    # Legacy import helpers (used by migration)
    # ------------------------------------------------------------------

    def _insert_legacy_import(
        self,
        conn: sqlite3.Connection,
        *,
        source_kind: str,
        source_path: str,
        source_sha256: str,
        record_key: str,
        payload: Any,
        imported_at: str,
    ) -> bool:
        """Insert one legacy record. Returns True if newly inserted."""

        try:
            conn.execute(
                """
                INSERT INTO legacy_imports (
                    source_kind, source_path, source_sha256, record_key,
                    payload_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_kind,
                    source_path,
                    source_sha256,
                    record_key,
                    canonical_json(payload),
                    imported_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def migration_completed(self, name: str = LEGACY_MIGRATION_NAME) -> bool:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT 1 FROM migration_receipts WHERE name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def get_migration_receipt(
        self, name: str = LEGACY_MIGRATION_NAME
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM migration_receipts WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        data = _row_to_dict(row) or {}
        data["manifest"] = json.loads(data.pop("manifest_json") or "{}")
        data["stats"] = json.loads(data.pop("stats_json") or "{}")
        return data

    def count_legacy_imports(self, source_kind: str | None = None) -> int:
        self.ensure_schema()
        if source_kind is None:
            row = self._connect().execute(
                "SELECT COUNT(*) AS n FROM legacy_imports"
            ).fetchone()
        else:
            row = self._connect().execute(
                "SELECT COUNT(*) AS n FROM legacy_imports WHERE source_kind = ?",
                (source_kind,),
            ).fetchone()
        return int(row["n"])

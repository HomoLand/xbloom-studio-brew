"""Recipe, revision, and catalog envelope methods for StateStore."""

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


class StateStoreCatalogMixin:
    """Catalog, recipe, and revision storage methods mixed into StateStore."""

    # ------------------------------------------------------------------
    # Recipe primitives
    # ------------------------------------------------------------------

    def get_recipe(self, recipe_id: str) -> dict[str, Any] | None:
        """Return one recipe row with parsed provenance/metadata, or None."""

        self.ensure_schema()
        row = self._connect().execute(
            "SELECT * FROM recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_recipe_row(row)

    def upsert_recipe(
        self,
        *,
        recipe_id: str | None = None,
        kind: str | None = None,
        name: str | None = None,
        source: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert or update a recipe; omitted fields preserve stored values.

        When ``provenance`` / ``metadata`` are omitted (``None``), existing JSON
        columns are left unchanged on update (SQL COALESCE). The return value is
        the actual stored row after the write.

        Low-level primitive: does not validate content or sanitize provenance
        (used by legacy import / bridge). Prefer
        :meth:`create_recipe_with_revision` for catalog writes.
        """

        rid = recipe_id or f"rcp_{uuid4().hex}"
        now = utc_now()
        prov_json = (
            canonical_json(dict(provenance)) if provenance is not None else None
        )
        meta_json = canonical_json(dict(metadata)) if metadata is not None else None
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_id = ?",
                (rid,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE recipes SET
                        kind = COALESCE(?, kind),
                        name = COALESCE(?, name),
                        updated_at = ?,
                        source = COALESCE(?, source),
                        provenance_json = COALESCE(?, provenance_json),
                        metadata_json = COALESCE(?, metadata_json)
                    WHERE recipe_id = ?
                    """,
                    (kind, name, now, source, prov_json, meta_json, rid),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO recipes (
                        recipe_id, kind, name, created_at, updated_at,
                        source, provenance_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rid,
                        kind,
                        name,
                        now,
                        now,
                        source,
                        prov_json if prov_json is not None else canonical_json({}),
                        meta_json if meta_json is not None else canonical_json({}),
                    ),
                )
            row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?",
                (rid,),
            ).fetchone()
        return _parse_recipe_row(row)

    def add_recipe_revision(
        self,
        recipe_id: str,
        content: Mapping[str, Any],
        *,
        revision_id: str | None = None,
        parent_revision_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Low-level revision insert without domain validation or OCC.

        Prefer :meth:`create_recipe_revision` for Web/catalog edits (validates
        content, requires expected parent, updates recipe display fields).
        """

        rev_id = revision_id or f"rev_{uuid4().hex}"
        content_json = canonical_json(dict(content))
        digest = sha256_text(content_json)
        prov = canonical_json(dict(provenance or {}))
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown recipe_id {recipe_id!r}")
            if parent_revision_id is not None:
                parent = conn.execute(
                    "SELECT recipe_id FROM recipe_revisions WHERE revision_id = ?",
                    (parent_revision_id,),
                ).fetchone()
                if parent is None:
                    raise StorageError(
                        f"unknown parent_revision_id {parent_revision_id!r}"
                    )
                if parent["recipe_id"] != recipe_id:
                    raise StorageError(
                        f"parent_revision_id {parent_revision_id!r} belongs to a "
                        f"different recipe (expected {recipe_id!r})"
                    )
            max_row = conn.execute(
                "SELECT MAX(revision_number) AS n FROM recipe_revisions "
                "WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            number = int(max_row["n"] or 0) + 1
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev_id,
                        recipe_id,
                        number,
                        content_json,
                        digest,
                        parent_revision_id,
                        now,
                        prov,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"recipe revision integrity failure: {exc}"
                ) from exc
        return {
            "revision_id": rev_id,
            "recipe_id": recipe_id,
            "revision_number": number,
            "content_sha256": digest,
            "parent_revision_id": parent_revision_id,
            "created_at": now,
            "provenance": dict(provenance or {}),
            "content": dict(content),
        }

    def get_recipe_revision(self, revision_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT r.*, recipes.kind AS recipe_kind, recipes.name AS recipe_name
            FROM recipe_revisions r
            LEFT JOIN recipes ON recipes.recipe_id = r.recipe_id
            WHERE r.revision_id = ?
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_revision_row(row)

    # ------------------------------------------------------------------
    # High-level recipe / revision API (Phase B B8)
    # ------------------------------------------------------------------

    def create_recipe_with_revision(
        self,
        content: Mapping[str, Any] | Any,
        *,
        recipe_id: str | None = None,
        name: str | None = None,
        source: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        creation_source: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create one stable recipe and its first immutable revision.

        Validates/canonicalizes content **before** opening a write transaction.
        Returns ``{"recipe": ..., "revision": ...}``.
        """

        canonical, kind = canonicalize_recipe_content(content)
        safe_prov = sanitize_recipe_provenance(
            provenance, creation_source=creation_source
        )
        if metadata is not None and not isinstance(metadata, Mapping):
            raise StorageError("metadata must be a mapping")
        meta = dict(metadata or {})
        display_name = (
            str(name) if name is not None else str(canonical.get("name") or "Unnamed")
        )
        rid = recipe_id or f"rcp_{uuid4().hex}"
        rev_id = revision_id or f"rev_{uuid4().hex}"
        content_json = canonical_json(canonical)
        digest = sha256_text(content_json)
        prov_json = canonical_json(safe_prov)
        meta_json = canonical_json(meta)
        now = utc_now()

        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT recipe_id FROM recipes WHERE recipe_id = ?",
                (rid,),
            ).fetchone()
            if existing is not None:
                raise StorageConflictError(f"recipe_id already exists: {rid!r}")
            _maybe_recipe_write_fault("before_recipe_insert")
            conn.execute(
                """
                INSERT INTO recipes (
                    recipe_id, kind, name, created_at, updated_at,
                    source, provenance_json, metadata_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    rid,
                    kind,
                    display_name,
                    now,
                    now,
                    source,
                    prov_json,
                    meta_json,
                ),
            )
            _maybe_recipe_write_fault("before_revision_insert")
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at, provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (rev_id, rid, content_json, digest, now, prov_json),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageError(
                    f"recipe revision integrity failure: {exc}"
                ) from exc
            recipe_row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?", (rid,)
            ).fetchone()
            rev_row = conn.execute(
                "SELECT * FROM recipe_revisions WHERE revision_id = ?", (rev_id,)
            ).fetchone()

        return {
            "recipe": _parse_recipe_row(recipe_row),
            "revision": _parse_revision_row(rev_row),
        }

    def create_recipe_revision(
        self,
        recipe_id: str,
        content: Mapping[str, Any] | Any,
        *,
        expected_parent_revision_id: str,
        name: str | None = None,
        source: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        creation_source: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a new revision with mandatory expected-parent OCC.

        Requires ``expected_parent_revision_id`` to match the current latest
        revision. Updates recipe display fields / provenance / metadata in the
        same ``BEGIN IMMEDIATE`` transaction. Raises
        :class:`StorageConflictError` on stale parent (no lost update, no
        duplicate ``revision_number``).
        """

        if not expected_parent_revision_id:
            raise StorageError("expected_parent_revision_id is required")
        canonical, kind = canonicalize_recipe_content(content)
        safe_prov = sanitize_recipe_provenance(
            provenance,
            parent_revision_id=expected_parent_revision_id,
            creation_source=creation_source,
        )
        if metadata is not None and not isinstance(metadata, Mapping):
            raise StorageError("metadata must be a mapping")
        display_name = (
            str(name) if name is not None else str(canonical.get("name") or "Unnamed")
        )
        rev_id = revision_id or f"rev_{uuid4().hex}"
        content_json = canonical_json(canonical)
        digest = sha256_text(content_json)
        prov_json = canonical_json(safe_prov)
        now = utc_now()

        with self.transaction() as conn:
            recipe_row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if recipe_row is None:
                raise StorageError(f"unknown recipe_id {recipe_id!r}")

            latest = conn.execute(
                """
                SELECT revision_id, revision_number FROM recipe_revisions
                WHERE recipe_id = ?
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                (recipe_id,),
            ).fetchone()
            if latest is None:
                raise StorageError(
                    f"recipe {recipe_id!r} has no revisions; use "
                    "create_recipe_with_revision"
                )
            if latest["revision_id"] != expected_parent_revision_id:
                raise StorageConflictError(
                    f"expected parent revision {expected_parent_revision_id!r} "
                    f"but latest is {latest['revision_id']!r}"
                )
            number = int(latest["revision_number"]) + 1

            meta_json: str | None
            if metadata is not None:
                meta_json = canonical_json(dict(metadata))
            else:
                meta_json = None

            _maybe_recipe_write_fault("before_revision_insert")
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at, provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rev_id,
                        recipe_id,
                        number,
                        content_json,
                        digest,
                        expected_parent_revision_id,
                        now,
                        prov_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Concurrent winner already took this revision_number.
                raise StorageConflictError(
                    f"recipe revision conflict for {recipe_id!r}: {exc}"
                ) from exc

            _maybe_recipe_write_fault("before_recipe_update")
            conn.execute(
                """
                UPDATE recipes SET
                    kind = ?,
                    name = ?,
                    updated_at = ?,
                    source = COALESCE(?, source),
                    provenance_json = ?,
                    metadata_json = COALESCE(?, metadata_json)
                WHERE recipe_id = ?
                """,
                (
                    kind,
                    display_name,
                    now,
                    source,
                    prov_json,
                    meta_json,
                    recipe_id,
                ),
            )
            recipe_out = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()
            rev_out = conn.execute(
                "SELECT * FROM recipe_revisions WHERE revision_id = ?", (rev_id,)
            ).fetchone()

        return {
            "recipe": _parse_recipe_row(recipe_out),
            "revision": _parse_revision_row(rev_out),
        }

    def get_latest_recipe_revision(self, recipe_id: str) -> dict[str, Any] | None:
        """Return the latest revision for *recipe_id*, or None."""

        self.ensure_schema()
        row = self._connect().execute(
            """
            SELECT * FROM recipe_revisions
            WHERE recipe_id = ?
            ORDER BY revision_number DESC
            LIMIT 1
            """,
            (recipe_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_revision_row(row)

    def list_recipe_revisions(self, recipe_id: str) -> list[dict[str, Any]]:
        """List revisions for *recipe_id* in stable ``revision_number`` order."""

        self.ensure_schema()
        rows = self._connect().execute(
            """
            SELECT * FROM recipe_revisions
            WHERE recipe_id = ?
            ORDER BY revision_number ASC
            """,
            (recipe_id,),
        ).fetchall()
        return [_parse_revision_row(row) for row in rows]

    def list_recipes(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = DEFAULT_RECIPE_LIST_LIMIT,
        offset: int = 0,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List recipes with latest revision summary/content.

        Filters: optional storage *kind* (``coffee``/``tea``), case-insensitive
        name/id *query*, pagination bounds, and *include_archived*.
        """

        if not isinstance(limit, int) or not (1 <= limit <= MAX_RECIPE_LIST_LIMIT):
            raise StorageError(
                f"limit must be an integer 1..{MAX_RECIPE_LIST_LIMIT}, got {limit!r}"
            )
        if not isinstance(offset, int) or offset < 0:
            raise StorageError(f"offset must be a non-negative integer, got {offset!r}")

        self.ensure_schema()
        clauses: list[str] = []
        params: list[Any] = []
        if not include_archived:
            clauses.append("r.archived_at IS NULL")
        if kind is not None:
            clauses.append("r.kind = ?")
            params.append(kind)
        if query is not None and str(query).strip():
            like = f"%{str(query).strip()}%"
            clauses.append("(r.name LIKE ? COLLATE NOCASE OR r.recipe_id LIKE ? COLLATE NOCASE)")
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT r.*,
                   rev.revision_id AS latest_revision_id,
                   rev.revision_number AS latest_revision_number,
                   rev.content_json AS latest_content_json,
                   rev.content_sha256 AS latest_content_sha256,
                   rev.parent_revision_id AS latest_parent_revision_id,
                   rev.created_at AS latest_revision_created_at,
                   rev.provenance_json AS latest_provenance_json
            FROM recipes r
            LEFT JOIN recipe_revisions rev
              ON rev.recipe_id = r.recipe_id
             AND rev.revision_number = (
                    SELECT MAX(rr.revision_number)
                    FROM recipe_revisions rr
                    WHERE rr.recipe_id = r.recipe_id
             )
            {where}
            ORDER BY r.updated_at DESC, r.recipe_id ASC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        rows = self._connect().execute(sql, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            data = _parse_recipe_row(row)
            latest: dict[str, Any] | None = None
            if data.get("latest_revision_id"):
                latest = {
                    "revision_id": data.pop("latest_revision_id"),
                    "recipe_id": data["recipe_id"],
                    "revision_number": data.pop("latest_revision_number"),
                    "content": json.loads(data.pop("latest_content_json")),
                    "content_sha256": data.pop("latest_content_sha256"),
                    "parent_revision_id": data.pop("latest_parent_revision_id"),
                    "created_at": data.pop("latest_revision_created_at"),
                    "provenance": json.loads(
                        data.pop("latest_provenance_json") or "{}"
                    ),
                }
            else:
                for key in (
                    "latest_revision_id",
                    "latest_revision_number",
                    "latest_content_json",
                    "latest_content_sha256",
                    "latest_parent_revision_id",
                    "latest_revision_created_at",
                    "latest_provenance_json",
                ):
                    data.pop(key, None)
            data["latest_revision"] = latest
            result.append(data)
        return result

    def archive_recipe(
        self,
        recipe_id: str,
        *,
        expected_latest_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Soft-archive a recipe without deleting revisions or breaking workflows.

        When *expected_latest_revision_id* is provided it must match the current
        latest revision (guards against archiving over a newer browser edit).
        """

        return self._set_recipe_archived(
            recipe_id,
            archived=True,
            expected_latest_revision_id=expected_latest_revision_id,
        )

    def restore_recipe(
        self,
        recipe_id: str,
        *,
        expected_latest_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore a previously archived recipe (revisions unchanged)."""

        return self._set_recipe_archived(
            recipe_id,
            archived=False,
            expected_latest_revision_id=expected_latest_revision_id,
        )

    def _set_recipe_archived(
        self,
        recipe_id: str,
        *,
        archived: bool,
        expected_latest_revision_id: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"unknown recipe_id {recipe_id!r}")
            if expected_latest_revision_id is not None:
                latest = conn.execute(
                    """
                    SELECT revision_id FROM recipe_revisions
                    WHERE recipe_id = ?
                    ORDER BY revision_number DESC
                    LIMIT 1
                    """,
                    (recipe_id,),
                ).fetchone()
                latest_id = latest["revision_id"] if latest is not None else None
                if latest_id != expected_latest_revision_id:
                    raise StorageConflictError(
                        f"expected latest revision {expected_latest_revision_id!r} "
                        f"but latest is {latest_id!r}"
                    )
            archived_at = now if archived else None
            conn.execute(
                """
                UPDATE recipes SET archived_at = ?, updated_at = ?
                WHERE recipe_id = ?
                """,
                (archived_at, now, recipe_id),
            )
            out = conn.execute(
                "SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()
        return _parse_recipe_row(out)

    # ------------------------------------------------------------------
    # Catalog view / merge (Phase 0.3/0.4 catalog cutover)
    # ------------------------------------------------------------------

    def build_catalog_snapshot(
        self,
        *,
        include_derived: bool = True,
        include_archived: bool = False,
        schema_version: int = 1,
    ) -> dict[str, Any]:
        """Build a public catalog dict from recipes + latest revisions.

        Catalog-owned rows (metadata.catalog_entry_id / legacy mapping) keep the
        full normalized entry envelope. Web-created recipes without catalog
        metadata appear as derived public entries when *include_derived* is set.

        When ``state.db`` does not exist yet, returns an empty snapshot without
        creating the database (read path must not invent runtime files).
        """

        if not self.db_path.is_file():
            return {
                "schema_version": schema_version,
                "created_at": None,
                "updated_at": None,
                "entries": [],
                "path": str(self.db_path),
                "source": "state.db",
                "authoritative": "sqlite",
                "exists": False,
            }
        self.ensure_schema()
        rows = self.list_recipes(
            limit=MAX_RECIPE_LIST_LIMIT,
            offset=0,
            include_archived=include_archived,
        )
        # Paginate if needed (personal catalogs are small; hard-cap safety).
        if len(rows) >= MAX_RECIPE_LIST_LIMIT:
            offset = MAX_RECIPE_LIST_LIMIT
            while True:
                batch = self.list_recipes(
                    limit=MAX_RECIPE_LIST_LIMIT,
                    offset=offset,
                    include_archived=include_archived,
                )
                if not batch:
                    break
                rows.extend(batch)
                if len(batch) < MAX_RECIPE_LIST_LIMIT:
                    break
                offset += MAX_RECIPE_LIST_LIMIT

        entries: list[dict[str, Any]] = []
        updated_at: str | None = None
        created_at: str | None = None
        for row in rows:
            entry = self._catalog_entry_from_recipe_row(row, include_derived=include_derived)
            if entry is None:
                continue
            entries.append(entry)
            ru = row.get("updated_at")
            rc = row.get("created_at")
            if isinstance(ru, str) and (updated_at is None or ru > updated_at):
                updated_at = ru
            if isinstance(rc, str) and (created_at is None or rc < created_at):
                created_at = rc
        entries.sort(
            key=lambda item: (
                str(item.get("kind") or ""),
                str(item.get("name") or "").casefold(),
                str(item.get("id") or ""),
            )
        )
        return {
            "schema_version": schema_version,
            "created_at": created_at,
            "updated_at": updated_at,
            "entries": entries,
            "path": str(self.db_path),
            "source": "state.db",
            "authoritative": "sqlite",
            "exists": True,
        }

    def get_catalog_entry(
        self,
        identifier: str,
        *,
        include_derived: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any] | None:
        """Resolve one catalog entry by public id, table_id, or unambiguous name."""

        catalog = self.build_catalog_snapshot(
            include_derived=include_derived,
            include_archived=include_archived,
        )
        entries = list(catalog.get("entries") or [])
        exact = [e for e in entries if str(e.get("id")) == identifier]
        if exact:
            return dict(exact[0])
        by_table = [
            e
            for e in entries
            if e.get("table_id") is not None and str(e.get("table_id")) == identifier
        ]
        if len(by_table) == 1:
            return dict(by_table[0])
        matches = [
            e
            for e in entries
            if identifier.casefold() in str(e.get("name", "")).casefold()
        ]
        if len(matches) == 1:
            return dict(matches[0])
        if matches:
            raise StorageError(
                f"catalog identifier {identifier!r} is ambiguous ({len(matches)} matches)"
            )
        return None

    def list_catalog_entries(
        self,
        *,
        kind: str = "all",
        origin: str | None = None,
        query: str | None = None,
        executable_only: bool = False,
        slot_compatible_only: bool = False,
        include_derived: bool = True,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List summary catalog entries with the same filters as the Skill CLI."""

        catalog = self.build_catalog_snapshot(
            include_derived=include_derived,
            include_archived=include_archived,
        )
        needle = (query or "").strip().casefold()
        result: list[dict[str, Any]] = []
        for entry in catalog.get("entries") or []:
            if kind != "all" and entry.get("kind") != kind:
                continue
            if origin and entry.get("origin") != origin:
                continue
            if executable_only and not entry.get("executable"):
                continue
            if slot_compatible_only and not entry.get("slot_compatible"):
                continue
            haystack = " ".join(
                str(entry.get(key, ""))
                for key in ("id", "name", "origin", "author", "cup_type")
            ).casefold()
            if needle and needle not in haystack:
                continue
            result.append(
                {
                    key: entry.get(key)
                    for key in (
                        "id",
                        "table_id",
                        "name",
                        "kind",
                        "machine_program",
                        "origin",
                        "author",
                        "cup_type",
                        "executable",
                        "slot_compatible",
                        "slots",
                        "warnings",
                        "validation_errors",
                        "manual_preparation",
                        "catalog_owned",
                        "derived",
                        "recipe_id",
                    )
                    if entry.get(key) not in (None, [], "")
                }
            )
        return result

    def catalog_summary(self, *, include_derived: bool = True) -> dict[str, Any]:
        """Counts for catalog status CLI / doctor."""

        catalog = self.build_catalog_snapshot(include_derived=include_derived)
        entries = list(catalog.get("entries") or [])
        return {
            "total": len(entries),
            "coffee": sum(entry.get("kind") == "coffee" for entry in entries),
            "tea": sum(entry.get("kind") == "tea" for entry in entries),
            "executable": sum(bool(entry.get("executable")) for entry in entries),
            "slot_compatible": sum(
                bool(entry.get("slot_compatible")) for entry in entries
            ),
            "updated_at": catalog.get("updated_at"),
            "path": str(self.db_path),
            "source": "state.db",
            "authoritative": "sqlite",
            "exists": bool(catalog.get("exists", self.db_path.is_file())),
        }

    def merge_catalog_entries(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        source: str = CATALOG_SOURCE_MERGE,
        creation_source: str | None = None,
    ) -> dict[str, Any]:
        """Transactionally merge normalized catalog entries into recipes/revisions.

        Rules (BEGIN IMMEDIATE):
        - create recipe + first revision atomically for new entry ids
        - changed recipe content creates one new immutable child revision
        - metadata-only changes update recipe metadata without a fake revision
        - unchanged replay is idempotent (no duplicate revisions)
        - never deletes another writer's revisions; concurrent writers serialize
          on IMMEDIATE and parent/revision_number uniqueness
        """

        stats: dict[str, Any] = {
            "candidates": 0,
            "created": 0,
            "updated": 0,
            "metadata_only": 0,
            "unchanged": 0,
            "skipped": 0,
        }
        now = utc_now()
        # One BEGIN IMMEDIATE transaction: any failure rolls back the whole batch.
        with self.transaction() as conn:
            for raw in entries:
                stats["candidates"] += 1
                if not isinstance(raw, Mapping):
                    raise StorageError("catalog entry must be a mapping")
                if raw.get("derived") or raw.get("catalog_owned") is False:
                    stats["skipped"] += 1
                    continue
                action = self._merge_one_catalog_entry_in_tx(
                    conn,
                    dict(raw),
                    now=now,
                    source=source,
                    creation_source=creation_source,
                )
                if action == "created":
                    stats["created"] += 1
                elif action == "updated":
                    stats["updated"] += 1
                elif action == "metadata_only":
                    stats["metadata_only"] += 1
                else:
                    stats["unchanged"] += 1
        return stats

    def archive_catalog_entry(
        self,
        *,
        entry_id: str | None = None,
        table_id: int | str | None = None,
        expected_latest_revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Soft-archive one local catalog-owned entry after confirmed cloud delete.

        Targets exact catalog-owned mappings only:
        - ``entry_id`` is exact id match (never substring / name fallback)
        - ``table_id`` is exact; ambiguity fails
        - derived Web recipes are never archived through this API

        Does not delete recipe_revisions or history_events.
        """

        catalog = self.build_catalog_snapshot(
            include_derived=False, include_archived=True
        )
        entries = list(catalog.get("entries") or [])
        entry: dict[str, Any] | None = None
        if entry_id is not None:
            eid = str(entry_id).strip()
            if not eid:
                raise StorageError("entry_id must be a non-empty string")
            exact = [e for e in entries if str(e.get("id")) == eid]
            if not exact:
                raise StorageError(
                    f"catalog entry {eid!r} not found for archive "
                    "(exact catalog-owned id required; no name fallback)"
                )
            entry = exact[0]
        elif table_id is not None:
            matches = [
                e
                for e in entries
                if e.get("table_id") is not None
                and str(e.get("table_id")) == str(table_id)
            ]
            if len(matches) > 1:
                raise StorageError(
                    f"catalog table_id {table_id!r} is ambiguous ({len(matches)} matches)"
                )
            if not matches:
                raise StorageError(
                    f"catalog table_id {table_id!r} not found for archive"
                )
            entry = matches[0]
        else:
            raise StorageError(
                "entry_id or table_id is required to archive catalog entry"
            )
        if entry.get("catalog_owned") is False or entry.get("derived"):
            raise StorageError(
                "archive_catalog_entry refuses derived / non-owned recipes"
            )
        recipe_id = entry.get("recipe_id") or recipe_id_for_catalog_entry_id(
            str(entry["id"])
        )
        archived = self.archive_recipe(
            str(recipe_id),
            expected_latest_revision_id=expected_latest_revision_id,
        )
        return {
            "archived": True,
            "entry_id": entry.get("id"),
            "recipe_id": archived.get("recipe_id"),
            "table_id": entry.get("table_id"),
            "archived_at": archived.get("archived_at"),
        }

    def _catalog_entry_from_recipe_row(
        self,
        row: Mapping[str, Any],
        *,
        include_derived: bool,
    ) -> dict[str, Any] | None:
        metadata = dict(row.get("metadata") or {})
        provenance = dict(row.get("provenance") or {})
        latest = row.get("latest_revision")
        recipe_body: dict[str, Any] = {}
        if isinstance(latest, Mapping) and isinstance(latest.get("content"), Mapping):
            recipe_body = dict(latest["content"])

        owned_ids = catalog_ownership_entry_ids(metadata, provenance)
        entry_id: str | None = None
        if len(owned_ids) == 1:
            entry_id = next(iter(owned_ids))
        elif metadata.get("catalog_entry_id"):
            entry_id = str(metadata.get("catalog_entry_id")).strip() or None
        elif provenance.get("catalog_entry_id"):
            entry_id = str(provenance.get("catalog_entry_id")).strip() or None
        elif provenance.get("legacy_entry_id"):
            entry_id = str(provenance.get("legacy_entry_id")).strip() or None
        # Never treat a bare legacy_ recipe_id prefix as catalog ownership.

        envelope = metadata.get("catalog_envelope")
        if entry_id and isinstance(envelope, Mapping):
            entry = normalize_catalog_envelope(envelope)
            entry["id"] = str(entry_id)
            entry["recipe"] = recipe_body
            entry["recipe_id"] = row.get("recipe_id")
            entry["catalog_owned"] = True
            entry["derived"] = False
            if row.get("archived_at"):
                entry["archived_at"] = row.get("archived_at")
            # Prefer live name/kind from recipe row when present.
            if row.get("name"):
                entry["name"] = row.get("name")
            if row.get("kind") in {"coffee", "tea"}:
                entry["kind"] = row.get("kind")
            return entry

        # Partial legacy import: ownership markers present, body in revision.
        # provenance.sources alone (without ownership markers) is not enough.
        if entry_id and (
            "executable" in metadata
            or "slot_compatible" in metadata
            or provenance.get("legacy_entry_id")
            or provenance.get("catalog_entry_id")
            or metadata.get("catalog_entry_id")
        ):
            entry = {
                "id": str(entry_id),
                "name": row.get("name") or recipe_body.get("name") or str(entry_id),
                "kind": row.get("kind") or recipe_body.get("kind") or "coffee",
                "executable": metadata.get("executable"),
                "slot_compatible": metadata.get("slot_compatible"),
                "origin": provenance.get("origin"),
                "sources": list(provenance.get("sources") or []),
                "recipe": recipe_body,
                "recipe_id": row.get("recipe_id"),
                "catalog_owned": True,
                "derived": False,
            }
            return entry

        if not include_derived:
            return None
        # Web / high-level API recipes without catalog ownership markers.
        recipe_id = str(row.get("recipe_id") or "")
        if not recipe_id:
            return None
        kind = row.get("kind") or recipe_body.get("kind") or "coffee"
        name = row.get("name") or recipe_body.get("name") or recipe_id
        executable = False
        validation_errors: list[str] = []
        if recipe_body:
            try:
                canonicalize_recipe_content(recipe_body)
                executable = True
            except StorageError as exc:
                validation_errors.append(str(exc))
        return {
            "id": recipe_id,
            "name": name,
            "kind": kind if kind in {"coffee", "tea"} else "coffee",
            "machine_program": (
                "omni-tea-brewer" if kind == "tea" else "coffee-pour-over"
            ),
            "origin": provenance.get("origin") or row.get("source") or CATALOG_SOURCE_WEB,
            "executable": executable,
            "slot_compatible": False,
            "validation_errors": validation_errors,
            "warnings": [],
            "sources": [
                {
                    "type": "web-state",
                    "recipe_id": recipe_id,
                }
            ],
            "recipe": recipe_body,
            "recipe_id": recipe_id,
            "catalog_owned": False,
            "derived": True,
            "slots": [],
        }

    @staticmethod
    def _require_catalog_merge_ownership(
        entry_id: str,
        recipe_id: str,
        metadata: Mapping[str, Any],
        provenance: Mapping[str, Any],
    ) -> None:
        """Refuse to take over a row not explicitly owned by this catalog entry."""

        owned = catalog_ownership_entry_ids(metadata, provenance)
        if not owned:
            raise StorageConflictError(
                f"recipe_id {recipe_id!r} exists without catalog ownership "
                f"markers; refusing merge of catalog entry {entry_id!r}"
            )
        if owned != {entry_id}:
            raise StorageConflictError(
                f"catalog entry {entry_id!r} conflicts with existing ownership "
                f"markers {sorted(owned)!r} on recipe_id {recipe_id!r}"
            )

    def _merge_one_catalog_entry_in_tx(
        self,
        conn: sqlite3.Connection,
        entry: dict[str, Any],
        *,
        now: str,
        source: str,
        creation_source: str | None,
    ) -> str:
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            raise StorageError("catalog entry missing id")
        envelope, recipe_body = split_catalog_entry(entry)
        recipe_id = recipe_id_for_catalog_entry_id(entry_id)
        content_json = canonical_json(recipe_body)
        digest = sha256_text(content_json)

        existing_row = conn.execute(
            "SELECT * FROM recipes WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
        existing_meta: dict[str, Any] = {}
        existing_prov: dict[str, Any] = {}
        if existing_row is not None:
            existing_meta = json.loads(existing_row["metadata_json"] or "{}")
            if not isinstance(existing_meta, dict):
                existing_meta = {}
            existing_prov = json.loads(existing_row["provenance_json"] or "{}")
            if not isinstance(existing_prov, dict):
                existing_prov = {}
            self._require_catalog_merge_ownership(
                entry_id, recipe_id, existing_meta, existing_prov
            )

        existing_envelope = existing_meta.get("catalog_envelope")
        if not isinstance(existing_envelope, Mapping):
            existing_envelope = {
                k: existing_meta[k]
                for k in ("executable", "slot_compatible")
                if k in existing_meta
            } or None
        merged_envelope = merge_catalog_envelopes(existing_envelope, envelope)
        kind = merged_envelope.get("kind") or recipe_body.get("kind")
        if kind not in {"coffee", "tea"}:
            kind = "tea" if str(recipe_body.get("kind", "")).lower() == "tea" else "coffee"
        name = str(merged_envelope.get("name") or recipe_body.get("name") or entry_id)

        # Preserve unrelated Web/user metadata and provenance annotations.
        metadata = dict(existing_meta)
        metadata["catalog_entry_id"] = entry_id
        metadata["catalog_envelope"] = normalize_catalog_envelope(merged_envelope)
        metadata["executable"] = merged_envelope.get("executable")
        metadata["slot_compatible"] = merged_envelope.get("slot_compatible")

        provenance = dict(existing_prov)
        provenance["catalog_entry_id"] = entry_id
        provenance["legacy_entry_id"] = entry_id
        if "sources" in merged_envelope:
            provenance["sources"] = merged_envelope.get("sources")
        if "origin" in merged_envelope:
            provenance["origin"] = merged_envelope.get("origin")
        provenance["source"] = source
        if creation_source:
            provenance["creation_source"] = creation_source
        meta_json = canonical_json(metadata)
        prov_json = canonical_json(provenance)

        if existing_row is None:
            conn.execute(
                """
                INSERT INTO recipes (
                    recipe_id, kind, name, created_at, updated_at,
                    source, provenance_json, metadata_json, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    recipe_id,
                    kind,
                    name,
                    now,
                    now,
                    source,
                    prov_json,
                    meta_json,
                ),
            )
            rev_id = f"legacy_rev_{entry_id}_{digest[:16]}"
            try:
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at,
                        provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (rev_id, recipe_id, content_json, digest, now, prov_json),
                )
            except sqlite3.IntegrityError:
                # A global deterministic revision-id collision must not leave
                # the newly inserted catalog recipe without revision 1.
                rev_id = f"rev_{uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO recipe_revisions (
                        revision_id, recipe_id, revision_number, content_json,
                        content_sha256, parent_revision_id, created_at,
                        provenance_json
                    ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                    """,
                    (rev_id, recipe_id, content_json, digest, now, prov_json),
                )
            return "created"

        latest = conn.execute(
            """
            SELECT revision_id, revision_number, content_sha256
            FROM recipe_revisions
            WHERE recipe_id = ?
            ORDER BY revision_number DESC
            LIMIT 1
            """,
            (recipe_id,),
        ).fetchone()

        content_same = latest is not None and latest["content_sha256"] == digest
        # Compare canonical forms so byte-stable stored JSON matches rebuilds.
        existing_meta_canon = canonical_json(existing_meta)
        existing_prov_canon = canonical_json(existing_prov)
        existing_source = existing_row["source"]
        existing_name = existing_row["name"]
        existing_kind = existing_row["kind"]
        existing_archived = existing_row["archived_at"]
        # COALESCE(?, source) with non-null source always takes the merge source.
        row_unchanged = (
            content_same
            and existing_kind == kind
            and existing_name == name
            and existing_source == source
            and existing_meta_canon == meta_json
            and existing_prov_canon == prov_json
            and existing_archived is None
        )
        if row_unchanged:
            # True idempotency: no UPDATE at all (updated_at, revision count, ...).
            return "unchanged"

        # Un-archive on re-merge so cloud re-import restores visibility.
        # Content-identical restore / envelope-only edits are metadata_only.
        conn.execute(
            """
            UPDATE recipes SET
                kind = ?,
                name = ?,
                updated_at = ?,
                source = COALESCE(?, source),
                provenance_json = ?,
                metadata_json = ?,
                archived_at = NULL
            WHERE recipe_id = ?
            """,
            (kind, name, now, source, prov_json, meta_json, recipe_id),
        )

        if latest is None:
            rev_id = f"legacy_rev_{entry_id}_{digest[:16]}"
            conn.execute(
                """
                INSERT INTO recipe_revisions (
                    revision_id, recipe_id, revision_number, content_json,
                    content_sha256, parent_revision_id, created_at,
                    provenance_json
                ) VALUES (?, ?, 1, ?, ?, NULL, ?, ?)
                """,
                (rev_id, recipe_id, content_json, digest, now, prov_json),
            )
            return "updated"

        if content_same:
            return "metadata_only"

        number = int(latest["revision_number"]) + 1
        rev_id = f"rev_{uuid4().hex}"
        try:
            conn.execute(
                """
                INSERT INTO recipe_revisions (
                    revision_id, recipe_id, revision_number, content_json,
                    content_sha256, parent_revision_id, created_at,
                    provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rev_id,
                    recipe_id,
                    number,
                    content_json,
                    digest,
                    latest["revision_id"],
                    now,
                    prov_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Concurrent writer took this revision_number; re-check content.
            latest2 = conn.execute(
                """
                SELECT revision_id, revision_number, content_sha256
                FROM recipe_revisions
                WHERE recipe_id = ?
                ORDER BY revision_number DESC
                LIMIT 1
                """,
                (recipe_id,),
            ).fetchone()
            if latest2 is not None and latest2["content_sha256"] == digest:
                return "unchanged"
            raise StorageConflictError(
                f"catalog merge conflict for {entry_id!r}: {exc}"
            ) from exc
        return "updated"

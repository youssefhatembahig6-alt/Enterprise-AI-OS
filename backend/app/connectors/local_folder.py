"""Desktop local-folder connector, backend half (M5, ADR 0503, FR-13).

The Electron app watches a folder the user picked and pushes idempotent events. The
**backend** decides who may read the result; operating-system permissions on the
user's machine are never mapped to enterprise access:

* **Ownership.** Registering a folder creates a ``local_folder`` source owned by the
  signed-in user. The owner's department becomes the department; classification is
  ``internal``, no department restriction, any country. Layer 4 (resource grant) is
  derived at authorization time: the owner, plus the groups the owner shared with
  (``source_grants``). Only groups the owner belongs to can be added.
* **Events.** ``upsert`` carries the file's relative path, size, SHA-256, a per-folder
  sequence number and the bytes; ``delete`` carries the path and sequence. Every event
  id is recorded (``local_folder_events``): a replay after an offline retry or a
  duplicate watcher event returns the first result and changes nothing.
* **Ordering.** Events for one source are serialised by locking the source row. An
  event whose sequence is not newer than the last applied sequence for that path is
  ``stale`` and ignored, so a late retry can never resurrect a deleted file or roll a
  file back.
* **Deduplication.** An upsert whose SHA-256 equals the current version is
  ``unchanged`` (no new version, no job).
* **Rules re-applied here.** Relative paths only (no ``..``, absolute or backslash
  paths); hidden, dependency, temporary and lock files are ``excluded``; formats
  outside the M3/code list are ``unsupported``; files over 10 MB are ``too_large`` and
  their bytes are refused; the bytes must match the declared size and SHA-256.
* **Deletes.** A delete tombstones the document (M3); deleting the folder source
  tombstones every document in it. Qdrant points are removed.

New content goes through the M3 ingestion path unchanged (permissions before any job,
staged generations, immutable versions).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.config import Settings
from app.connectors import paths
from app.db.engine import tenant_transaction
from app.documents import store
from app.documents.ingest import collection_name
from app.documents.parsers import CODE_EXTENSIONS, MAX_UPLOAD_BYTES, SUPPORTED, ParseFailed, detect
from app.documents.qdrant import QdrantClient, QdrantError
from app.documents.storage import ObjectStore, object_key
from app.reporting.model_client import ModelClient
from app.repositories.audit import record_audit_event
from app.repositories.identity import load_subject
from app.security.context import AccessContext
from app.security.policy import Capability, capability_scope
from app.tenancy.config import require_connector

logger = logging.getLogger("eaios.connectors.local_folder")
SOURCE = "local_folder"
MAX_EVENTS = 50
MAX_BATCH_BYTES = 12 * 1024 * 1024
DEFAULT_CLASSIFICATION = "internal"


class FolderNotFound(Exception):
    """Unknown, foreign, deleted, or not the caller's folder (uniform 404)."""


class FolderInvalid(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Event:
    event_id: UUID
    seq: int
    op: str  # upsert | delete
    path: str
    sha256: str | None = None
    size: int | None = None
    content_base64: str | None = None


class LocalFolderService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        objects: ObjectStore,
        qdrant: QdrantClient,
        model: ModelClient,
        dispatch: Callable[[UUID, UUID], None] | None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._objects = objects
        self._qdrant = qdrant
        self._model = model
        self._dispatch = dispatch

    def _tx(self, ctx: AccessContext) -> Any:
        return tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id)

    # -- folders ----------------------------------------------------------------------------

    _VIEW = """
        SELECT s.id, s.name, s.external_root, s.external_ref, s.status, s.department,
               s.classification, s.owner_user_id, s.created_at,
               (s.kind = ANY (coalesce(ts.enabled_connectors, '{}'))) AS connector_enabled,
               (SELECT count(*) FROM connector_items i WHERE i.source_id = s.id
                    AND i.state = 'active') AS active_items,
               (SELECT count(*) FROM connector_items i WHERE i.source_id = s.id
                    AND i.state NOT IN ('active', 'removed')) AS skipped_items,
               coalesce((SELECT array_agg(g.key ORDER BY g.key) FROM source_grants sg
                           JOIN groups g ON g.tenant_id = sg.tenant_id AND g.id = sg.group_id
                          WHERE sg.source_id = s.id), '{}') AS shared_groups
          FROM connector_sources s LEFT JOIN tenant_settings ts ON ts.tenant_id = s.tenant_id
         WHERE s.kind = 'local_folder' AND s.status <> 'deleted'
    """

    @staticmethod
    def _out(r: Any) -> dict[str, Any]:
        return {
            "source_id": str(r.id),
            "folder_id": r.external_root,
            "name": r.name,
            "device": r.external_ref,
            "status": r.status,
            "department": r.department,
            "classification": r.classification,
            "shared_groups": list(r.shared_groups),
            "active_items": int(r.active_items),
            "skipped_items": int(r.skipped_items),
            "access": "current" if r.connector_enabled and r.status == "active" else "paused",
            "created_at": r.created_at.isoformat(),
        }

    def _owned(
        self, conn: Connection, ctx: AccessContext, source_id: UUID, *, lock: bool = False
    ) -> Any:
        row = conn.execute(
            text(self._VIEW + " AND s.id = :id" + (" FOR UPDATE OF s" if lock else "")),
            {"id": source_id},
        ).one_or_none()
        if row is None or row.owner_user_id != ctx.user_id:
            raise FolderNotFound
        return row

    def register(
        self, ctx: AccessContext, folder_id: UUID, name: str, device: str
    ) -> dict[str, Any]:
        with self._tx(ctx) as conn:
            require_connector(conn, SOURCE)
            subject = load_subject(conn, ctx.user_id)
            if subject is None or not subject.active or not subject.department:
                raise FolderInvalid("owner_not_eligible", "Your account cannot own a folder.")
            if capability_scope(subject, Capability.RESOURCE_READ) == frozenset():
                raise FolderInvalid("owner_not_eligible", "Your account cannot read documents.")
            existing = conn.execute(
                text(
                    "SELECT id, owner_user_id, status FROM connector_sources"
                    " WHERE kind = 'local_folder' AND external_root = :root"
                ),
                {"root": str(folder_id)},
            ).one_or_none()
            if existing is not None:
                if existing.owner_user_id != ctx.user_id or existing.status == "deleted":
                    raise FolderInvalid("invalid_source", "This folder cannot be registered.")
                return self._out(self._owned(conn, ctx, existing.id))
            source_id = uuid.uuid4()
            conn.execute(
                text(
                    """
                    INSERT INTO connector_sources (id, tenant_id, kind, name, external_root,
                        external_ref, department, classification, department_restricted,
                        allowed_countries, owner_user_id, permissions_current, created_by)
                    VALUES (:id, :t, 'local_folder', :n, :root, :device, :dept, :cls, false,
                            ARRAY['*'], :u, true, :u)
                    """
                ),
                {
                    "id": source_id,
                    "t": ctx.tenant_id,
                    "n": name[:200],
                    "root": str(folder_id),
                    "device": device[:200] or None,
                    "dept": subject.department,
                    "cls": DEFAULT_CLASSIFICATION,
                    "u": ctx.user_id,
                },
            )
            self._audit(conn, ctx, "local_folder.register", "folder_registered", source_id)
            return self._out(self._owned(conn, ctx, source_id))

    def list_own(self, ctx: AccessContext) -> list[dict[str, Any]]:
        with self._tx(ctx) as conn:
            rows = conn.execute(
                text(self._VIEW + " AND s.owner_user_id = :u ORDER BY s.created_at"),
                {"u": ctx.user_id},
            ).all()
            return [self._out(r) for r in rows]

    def set_sharing(self, ctx: AccessContext, source_id: UUID, groups: list[str]) -> dict[str, Any]:
        """Replace the groups the folder is shared with. Only the owner, and only groups
        the owner belongs to (nobody can push their files into a group they are not in)."""

        with self._tx(ctx) as conn:
            self._owned(conn, ctx, source_id, lock=True)
            require_connector(conn, SOURCE)
            wanted = sorted(set(groups))
            rows = conn.execute(
                text(
                    "SELECT g.id, g.key FROM groups g JOIN group_memberships m"
                    " ON m.tenant_id = g.tenant_id AND m.group_id = g.id AND m.user_id = :u"
                    " WHERE g.key = ANY(:keys)"
                ),
                {"u": ctx.user_id, "keys": wanted},
            ).all()
            if len(rows) != len(wanted):
                raise FolderInvalid(
                    "invalid_group", "You can share only with groups you belong to."
                )
            current = {
                r.group_id
                for r in conn.execute(
                    text(
                        "SELECT group_id FROM source_grants WHERE source_id = :s"
                        " AND group_id IS NOT NULL"
                    ),
                    {"s": source_id},
                ).all()
            }
            target = {r.id for r in rows}
            for gid in current - target:
                conn.execute(
                    text("DELETE FROM source_grants WHERE source_id = :s AND group_id = :g"),
                    {"s": source_id, "g": gid},
                )
            for gid in sorted(target - current, key=str):
                conn.execute(
                    text(
                        "INSERT INTO source_grants (tenant_id, source_id, group_id, created_by)"
                        " VALUES (:t, :s, :g, :u)"
                    ),
                    {"t": ctx.tenant_id, "s": source_id, "g": gid, "u": ctx.user_id},
                )
            self._audit(
                conn,
                ctx,
                "local_folder.sharing",
                "folder_sharing_set",
                source_id,
                groups=len(target),
            )
            return self._out(self._owned(conn, ctx, source_id))

    def items(self, ctx: AccessContext, source_id: UUID) -> list[dict[str, Any]]:
        with self._tx(ctx) as conn:
            self._owned(conn, ctx, source_id)
            return [
                {
                    "path": r.path,
                    "state": r.state,
                    "reason": r.reason,
                    "sha256": r.content_sha256,
                    "last_seq": r.last_seq,
                    "document_id": str(r.document_id) if r.document_id else None,
                }
                for r in conn.execute(
                    text(
                        "SELECT path, state, reason, content_sha256, last_seq, document_id"
                        " FROM connector_items WHERE source_id = :s ORDER BY path"
                    ),
                    {"s": source_id},
                ).all()
            ]

    def delete(self, ctx: AccessContext, source_id: UUID) -> None:
        """Owner removes the folder: the source is deleted and every document in it is
        tombstoned (citations stop opening, search excludes them at once)."""

        removed: list[UUID] = []
        with self._tx(ctx) as conn:
            self._owned(conn, ctx, source_id, lock=True)
            removed = tombstone_source(conn, source_id)
            self._audit(
                conn,
                ctx,
                "local_folder.delete",
                "folder_deleted",
                source_id,
                documents=len(removed),
            )
        self.drop_points(removed)

    # -- events -----------------------------------------------------------------------------

    def apply_events(
        self, ctx: AccessContext, source_id: UUID, events: list[Event]
    ) -> list[dict[str, Any]]:
        if len(events) > MAX_EVENTS:
            raise FolderInvalid("batch_too_large", f"At most {MAX_EVENTS} events per batch.")
        total = sum(len(e.content_base64 or "") * 3 // 4 for e in events)
        if total > MAX_BATCH_BYTES:
            raise FolderInvalid("batch_too_large", "The batch is larger than 12 MB.")
        with self._tx(ctx) as conn:
            self._owned(conn, ctx, source_id)
            require_connector(conn, SOURCE)
        results: list[dict[str, Any]] = []
        jobs: list[UUID] = []
        dropped: list[UUID] = []
        for event in events:
            result, job, drop = self._apply(ctx, source_id, event)
            results.append(result)
            if job is not None:
                jobs.append(job)
            if drop is not None:
                dropped.append(drop)
        if self._dispatch is not None:
            for job_id in jobs:
                self._dispatch(job_id, ctx.tenant_id)
        self.drop_points(dropped)
        return results

    def _apply(  # noqa: PLR0915 - one decision table per event
        self, ctx: AccessContext, source_id: UUID, event: Event
    ) -> tuple[dict[str, Any], UUID | None, UUID | None]:
        out: dict[str, Any] = {"event_id": str(event.event_id), "path": event.path}
        try:
            path = paths.check_relative(event.path)
        except paths.InvalidPath as exc:
            return {**out, "result": "rejected", "reason": f"invalid_path:{exc}"}, None, None
        data: bytes | None = None
        if event.op == "upsert" and event.content_base64 is not None:
            try:
                data = base64.b64decode(event.content_base64, validate=True)
            except (binascii.Error, ValueError):
                return {**out, "result": "rejected", "reason": "invalid_content"}, None, None
        job: UUID | None = None
        drop: UUID | None = None
        with self._tx(ctx) as conn:
            source = conn.execute(
                text(
                    "SELECT id, tenant_id, status, department, classification,"
                    " department_restricted, allowed_countries, owner_user_id"
                    " FROM connector_sources WHERE id = :s AND kind = 'local_folder' FOR UPDATE"
                ),
                {"s": source_id},
            ).one_or_none()
            if source is None or source.status != "active" or source.owner_user_id != ctx.user_id:
                raise FolderNotFound
            previous = conn.execute(
                text(
                    "SELECT result FROM local_folder_events WHERE source_id = :s AND event_id = :e"
                ),
                {"s": source_id, "e": event.event_id},
            ).scalar_one_or_none()
            if previous is not None:
                return {**out, "result": "duplicate", "first_result": previous}, None, None
            item = conn.execute(
                text(
                    "SELECT i.document_id, i.content_sha256, i.state, i.last_seq, d.deleted_at,"
                    " d.current_version FROM connector_items i LEFT JOIN documents d"
                    "   ON d.tenant_id = i.tenant_id AND d.id = i.document_id"
                    " WHERE i.source_id = :s AND i.path = :p FOR UPDATE OF i"
                ),
                {"s": source_id, "p": path},
            ).one_or_none()
            live = item is not None and item.document_id is not None and item.deleted_at is None
            result, reason, state = "applied", None, "active"
            document_id = item.document_id if item is not None else None
            sha = event.sha256
            if item is not None and item.last_seq is not None and event.seq <= item.last_seq:
                result, reason = "stale", "older_sequence"
            elif event.op == "delete":
                if live:
                    store.delete_document(conn, item.document_id)
                    drop = item.document_id
                    result = "removed"
                else:
                    result = "noop"
                state = "removed"
            else:
                state, reason = self._classify(path, event, data)
                if state != "active":
                    result = state
                    if live:
                        store.delete_document(conn, item.document_id)
                        drop = item.document_id
                elif live and item.content_sha256 == sha:
                    result = "unchanged"
                else:
                    assert data is not None and sha is not None  # noqa: S101 - classify checked
                    filename = path.rsplit("/", 1)[-1][:255]
                    media_type = detect(filename, data).media_type
                    revision = {"path": path, "sha256": sha, "device_folder": str(source_id)}
                    if live:
                        version = int(item.current_version) + 1
                        key = object_key(ctx.tenant_id, SOURCE, item.document_id, version, filename)
                        self._objects.put(key, data)
                        _, job, _ = store.add_version(
                            conn,
                            tenant_id=ctx.tenant_id,
                            document_id=item.document_id,
                            filename=filename,
                            media_type=media_type,
                            byte_size=len(data),
                            byte_sha256=sha,
                            storage_key_for=lambda d, v: key,
                            created_by=ctx.user_id,
                            source_revision=revision,
                        )
                        result = "updated"
                    else:
                        document_id = uuid.uuid4()
                        key = object_key(ctx.tenant_id, SOURCE, document_id, 1, filename)
                        self._objects.put(key, data)
                        _, job, _ = store.create_document(
                            conn,
                            tenant_id=ctx.tenant_id,
                            title=_title(path),
                            filename=filename,
                            media_type=media_type,
                            byte_size=len(data),
                            byte_sha256=sha,
                            storage_key_for=lambda d, v: key,
                            permissions=store.Permissions(
                                classification=source.classification,
                                department=source.department,
                                department_restricted=source.department_restricted,
                                allowed_countries=tuple(source.allowed_countries),
                                grants=(),  # owner + shared groups are derived
                            ),
                            unknown_department=source.department,
                            created_by=ctx.user_id,
                            source=SOURCE,
                            document_id=document_id,
                            source_revision=revision,
                        )
                        conn.execute(
                            text("UPDATE resources SET source_id = :s WHERE id = :d"),
                            {"s": source_id, "d": document_id},
                        )
                        result = "created"
            if result != "stale":
                conn.execute(
                    text(
                        """
                        INSERT INTO connector_items (tenant_id, source_id, path, document_id, size,
                            content_sha256, state, reason, last_seq, removed_at)
                        VALUES (:t, :s, :p, :d, :size, :sha, :st, :reason, :seq,
                                CASE WHEN :st = 'removed' THEN clock_timestamp() END)
                        ON CONFLICT (tenant_id, source_id, path) DO UPDATE SET
                            document_id = CASE WHEN EXCLUDED.state = 'active'
                                               THEN EXCLUDED.document_id
                                               ELSE connector_items.document_id END,
                            size = EXCLUDED.size,
                            content_sha256 = CASE WHEN EXCLUDED.state = 'active'
                                                  THEN EXCLUDED.content_sha256 END,
                            state = EXCLUDED.state, reason = EXCLUDED.reason,
                            last_seq = EXCLUDED.last_seq, removed_at = EXCLUDED.removed_at,
                            updated_at = clock_timestamp()
                        """
                    ),
                    {
                        "t": ctx.tenant_id,
                        "s": source_id,
                        "p": path,
                        "d": document_id,
                        "size": event.size,
                        "sha": sha if state == "active" else None,
                        "st": state,
                        "reason": reason,
                        "seq": event.seq,
                    },
                )
            conn.execute(
                text(
                    "INSERT INTO local_folder_events (tenant_id, source_id, event_id, path, op,"
                    " result) VALUES (:t, :s, :e, :p, :op, :r)"
                ),
                {
                    "t": ctx.tenant_id,
                    "s": source_id,
                    "e": event.event_id,
                    "p": path,
                    "op": event.op,
                    "r": result,
                },
            )
        out.update(result=result)
        if reason:
            out["reason"] = reason
        if document_id is not None and result in ("created", "updated", "unchanged"):
            out["document_id"] = str(document_id)
        return out, job, drop

    @staticmethod
    def _classify(path: str, event: Event, data: bytes | None) -> tuple[str, str | None]:
        reason = paths.exclusion(path)
        if reason is not None:
            return "excluded", reason
        ext = paths.extension(path)
        if ext not in SUPPORTED and ext not in CODE_EXTENSIONS:
            return "unsupported", "file_type"
        if event.size is not None and event.size > MAX_UPLOAD_BYTES:
            return "too_large", "over_10_mb"
        if data is None or event.sha256 is None or event.size is None:
            return "unsupported", "content_missing"
        if len(data) > MAX_UPLOAD_BYTES:
            return "too_large", "over_10_mb"
        if len(data) != event.size or hashlib.sha256(data).hexdigest() != event.sha256:
            return "unsupported", "checksum_mismatch"
        if not data:
            return "unsupported", "empty_file"
        try:
            detect(path.rsplit("/", 1)[-1], data)
        except ParseFailed as exc:
            return "unsupported", exc.code
        return "active", None

    # -- helpers ----------------------------------------------------------------------------

    def _audit(
        self,
        conn: Connection,
        ctx: AccessContext,
        event_type: str,
        reason: str,
        source_id: UUID,
        **details: Any,
    ) -> None:
        record_audit_event(
            conn,
            request_id=ctx.request_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            event_type=event_type,
            action="resource.admin",
            decision="info",
            reason=reason,
            details={"source_id": str(source_id), **details},
        )

    def drop_points(self, document_ids: list[UUID]) -> None:
        for document_id in document_ids:
            try:
                self._qdrant.delete_points(
                    collection_name(self._settings, self._model),
                    {"must": [{"key": "document_id", "match": {"value": str(document_id)}}]},
                )
            except QdrantError:
                logger.warning("qdrant_delete_failed", extra={"document_id": str(document_id)})


def tombstone_source(conn: Connection, source_id: UUID) -> list[UUID]:
    """Mark a connector source deleted and tombstone all of its live documents."""

    conn.execute(
        text(
            "UPDATE connector_sources SET status = 'deleted', claim_run_id = NULL,"
            " sync_claimed_until = NULL, updated_at = now() WHERE id = :s"
        ),
        {"s": source_id},
    )
    ids = [
        r.id
        for r in conn.execute(
            text(
                "SELECT d.id FROM documents d JOIN resources r ON r.tenant_id = d.tenant_id"
                " AND r.id = d.id WHERE r.source_id = :s AND d.deleted_at IS NULL"
            ),
            {"s": source_id},
        ).all()
    ]
    for document_id in ids:
        store.delete_document(conn, document_id)
    conn.execute(
        text(
            "UPDATE connector_items SET state = 'removed', removed_at = clock_timestamp(),"
            " updated_at = clock_timestamp() WHERE source_id = :s AND state <> 'removed'"
        ),
        {"s": source_id},
    )
    return ids


def _title(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return (stem.strip() or "Untitled")[:300]


__all__ = ["Event", "FolderInvalid", "FolderNotFound", "LocalFolderService", "tombstone_source"]

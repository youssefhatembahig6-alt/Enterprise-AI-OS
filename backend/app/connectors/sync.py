"""Google Drive folder sync (ADR 0401, FR-10).

One sync run walks the source's folder tree and re-reads **every** file's permission
list. That is what makes a sync *verified*: only after every listed item's
permissions were read (or the item was explicitly denied because they could not be)
does ``last_verified_sync_at`` advance. Content is re-downloaded only when its
fingerprint changed, so unchanged files are not re-ingested (incremental updates).

Per item:

* **new**          resource + permissions + document + version 1 + ingestion job, in one
                   transaction (M3 ordering: permissions before any job exists)
* **content**      a new document version through the M3 ingestion path
* **sharing**      grants rewritten only when the resolved grants changed (policy epoch)
* **rename/move**  title and path updated; a file moved *out of* the folder tree is
                   handled like a deletion
* **removed**      trashed, deleted or moved out: M3 tombstone + Qdrant cleanup
* **unreadable**   permissions could not be read: ``permission_state = 'unknown'``
                   (denied) until a later sync reads them

Mapping Drive permissions to the reference policy (layer 4, resource grant): a
``user`` permission grants the tenant user with that exact email. A ``group``
permission grants a user only if the group email is explicitly linked to an internal
group, the user is in that internal group, **and** the user's current Google
membership was verified in this run (``list_group_members``). If Google membership
cannot be verified, the group permission grants nobody: a link alone never establishes
who belongs to a Google group. Grants are always direct user grants, so a later run
that cannot verify membership removes them. Domain and "anyone with the link"
permissions grant nobody, and so do unknown emails; all of these are recorded as
unresolved principals for the administrator. Department,
classification and country rules come from the source (Drive has no such fields).

Fencing: a run holds the source through ``claim_run_id`` and remembers the source's
``policy_version``. Every write transaction first re-locks the source row and checks
both; an admin policy change (which bumps the version and releases the claim) or a
newer worker that took over an expired lease makes the older run ``superseded``: it
can no longer write items or grants, tombstone documents, mark permissions current or
clear the newer claim.

Allowlist: the operator's ``allowed_roots`` are re-read and checked at the start of
every run, before any Drive request; a removed root fails the run (content denied).

Failure: if the root cannot be read or any folder cannot be listed, the run fails,
``permissions_current`` becomes false and every item of the source is denied until
a later sync succeeds. Independently, readers deny content once the last verified
sync is older than the tenant's staleness bound (scheduler stopped).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.connectors.drive import (
    READ_ROLES,
    ContentUnavailable,
    DriveError,
    DriveFile,
    DriveNotConfigured,
    DrivePermission,
    PermissionsUnavailable,
    RootNotAllowed,
    supported_type,
)
from app.connectors.fencing import (
    FencedSyncBase,
    SourceRow,
    SyncOutcome,
    SyncSuperseded,
    load_source,
)
from app.db.engine import tenant_transaction
from app.documents import store
from app.documents.parsers import ParseFailed, detect
from app.documents.storage import object_key
from app.repositories.audit import record_audit_event

__all__ = [
    "DriveSyncService",
    "SourceRow",
    "SyncOutcome",
    "SyncSuperseded",
    "load_source",
    "resolve_permissions",
]

logger = logging.getLogger("eaios.connectors.sync")
SOURCE = "google_drive"
MAX_DEPTH = 20
MAX_ITEMS = 2000


@dataclass(frozen=True, slots=True)
class Listed:
    file: DriveFile
    path: str
    parent: str


@dataclass(frozen=True, slots=True)
class Resolved:
    grants: tuple[tuple[str, UUID | None, UUID | None], ...]
    unresolved: tuple[dict[str, str], ...]
    # (user_id, internal group_id, Google group email): verified Google members. Each
    # becomes an allow only while the link and the internal membership still exist.
    group_grants: tuple[tuple[UUID, UUID, str], ...] = ()


def group_emails(perms: list[DrivePermission]) -> set[str]:
    return {
        (p.email or "").strip().lower()
        for p in perms
        if p.type == "group" and p.email and not p.deleted and p.role in READ_ROLES
    }


def resolve_permissions(
    conn: Connection,
    perms: list[DrivePermission],
    memberships: dict[str, frozenset[str] | None] | None = None,
) -> Resolved:
    """``memberships`` maps a Google group email to its verified member emails, or to
    ``None`` when membership could not be verified in this run."""

    memberships = memberships or {}
    grants: set[tuple[str, UUID | None, UUID | None]] = set()
    group_grants: set[tuple[UUID, UUID, str]] = set()
    unresolved: list[dict[str, str]] = []
    for p in perms:
        if p.deleted:
            continue
        if p.role not in READ_ROLES:
            unresolved.append({"type": p.type or "unknown", "reason": "role_not_read"})
            continue
        email = (p.email or "").strip().lower()
        if p.type == "user" and email:
            uid = conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email}).scalar()
            if uid is None:
                unresolved.append({"type": "user", "email": email, "reason": "no_tenant_user"})
            else:
                grants.add(("allow", uid, None))
        elif p.type == "group" and email:
            gid = conn.execute(
                text(
                    "SELECT group_id FROM external_group_links"
                    " WHERE provider = 'google' AND external_email = :e"
                ),
                {"e": email},
            ).scalar()
            members = memberships.get(email)
            if gid is None:
                unresolved.append({"type": "group", "email": email, "reason": "no_group_link"})
            elif members is None:
                unresolved.append(
                    {"type": "group", "email": email, "reason": "group_membership_unverified"}
                )
            elif members:
                for u in conn.execute(
                    text("SELECT id FROM users WHERE email = ANY(:emails)"),
                    {"emails": sorted(members)},
                ).all():
                    group_grants.add((u.id, gid, email))
        else:
            # domain / anyone-with-link: never widened to "every tenant user".
            unresolved.append({"type": p.type or "unknown", "reason": "not_mapped"})
    ordered = tuple(sorted(grants, key=lambda g: (g[0], str(g[1]), str(g[2]))))
    return Resolved(
        ordered,
        tuple(sorted(unresolved, key=lambda u: json.dumps(u, sort_keys=True))),
        tuple(sorted(group_grants, key=lambda g: (str(g[0]), str(g[1]), g[2]))),
    )


def write_group_grants(
    conn: Connection, tenant_id: UUID, resource_id: UUID, rows: tuple[tuple[UUID, UUID, str], ...]
) -> None:
    conn.execute(text("DELETE FROM drive_group_grants WHERE resource_id = :r"), {"r": resource_id})
    for user_id, group_id, email in rows:
        conn.execute(
            text(
                "INSERT INTO drive_group_grants (tenant_id, resource_id, user_id, group_id,"
                " external_email) VALUES (:t, :r, :u, :g, :e)"
            ),
            {"t": tenant_id, "r": resource_id, "u": user_id, "g": group_id, "e": email},
        )


def _fingerprint(source: SourceRow, resolved: Resolved) -> str:
    payload = {
        "grants": [[e, str(u), str(g)] for e, u, g in resolved.grants],
        "group_grants": [[str(u), str(g), e] for u, g, e in resolved.group_grants],
        "classification": source.classification,
        "department": source.department,
        "restricted": source.department_restricted,
        "countries": sorted(source.allowed_countries),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _title(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return (stem.strip() or "Untitled")[:300]


class DriveSyncService(FencedSyncBase):
    """Google Drive folder sync on the shared fenced-run base."""

    def sync(
        self,
        tenant_id: UUID,
        source_id: UUID,
        *,
        trigger: str = "scheduled",
        requested_by: UUID | None = None,
        run_id: UUID | None = None,
    ) -> SyncOutcome:
        outcome, source, slug = self._claim(tenant_id, source_id, trigger, requested_by, run_id)
        if source is None:
            return outcome
        jobs: list[UUID] = []
        seen: set[str] | None = None
        try:
            # Re-read the operator allowlist now: a removed root never reaches Drive.
            if source.external_root not in self._roots(slug):
                raise RootNotAllowed("root removed from the operator allowlist")
            client = self._clients(slug)
            if client is None:
                raise DriveNotConfigured("no credentials for tenant")
            listed = self._walk(client, source.external_root)
            seen = set()
            groups: dict[str, frozenset[str] | None] = {}
            for item in listed:
                seen.add(item.file.id)
                jobs.extend(self._apply(client, source, item, outcome, requested_by, groups))
            outcome.status = "succeeded"
        except SyncSuperseded:
            self._superseded(source, outcome)
            return outcome
        except DriveError as exc:
            outcome.status, outcome.error_code = "failed", exc.code
            logger.warning("drive_sync_failed", extra={"code": exc.code})
        except Exception:
            outcome.status, outcome.error_code = "failed", "sync_internal_error"
            logger.exception("drive_sync_internal_error")
        self._finish(source, outcome, requested_by, seen if outcome.status == "succeeded" else None)
        if self._dispatch is not None and outcome.status != "superseded":
            for job_id in jobs:
                self._dispatch(job_id, tenant_id)
        return outcome

    # -- walk / apply --------------------------------------------------------------------

    def _walk(self, client: Any, root_id: str) -> list[Listed]:
        root = client.get_file(root_id)
        if not root.is_folder or root.trashed:
            raise DriveError("root is not a folder")
        out: list[Listed] = []
        queue: list[tuple[str, str, int]] = [(root.id, "", 0)]
        visited = {root.id}
        while queue:
            folder_id, prefix, depth = queue.pop(0)
            for child in sorted(client.list_children(folder_id), key=lambda f: (f.name, f.id)):
                if child.trashed or child.id in visited:
                    continue
                visited.add(child.id)
                path = f"{prefix}/{child.name}"
                if child.is_folder:
                    if depth + 1 <= MAX_DEPTH:
                        queue.append((child.id, path, depth + 1))
                    continue
                out.append(Listed(child, path, folder_id))
                if len(out) > MAX_ITEMS:
                    raise DriveError("too many items in the source")
        return out

    def _existing(self, conn: Connection, source_id: UUID, external_id: str) -> Any:
        return conn.execute(
            text(
                "SELECT i.document_id, i.content_fingerprint, i.permissions_fingerprint, i.state,"
                " i.name, i.path, d.deleted_at, d.current_version"
                " FROM drive_items i LEFT JOIN documents d"
                "   ON d.tenant_id = i.tenant_id AND d.id = i.document_id"
                " WHERE i.source_id = :s AND i.external_id = :e"
            ),
            {"s": source_id, "e": external_id},
        ).one_or_none()

    def _apply(  # noqa: PLR0915 - one decision table per item
        self,
        client: Any,
        source: SourceRow,
        item: Listed,
        outcome: SyncOutcome,
        requested_by: UUID | None,
        groups: dict[str, frozenset[str] | None] | None = None,
    ) -> list[UUID]:
        f = item.file
        groups = groups if groups is not None else {}
        with tenant_transaction(self._engine, source.tenant_id, requested_by) as conn:
            existing = self._existing(conn, source.id, f.id)
        live_doc = (
            existing is not None
            and existing.document_id is not None
            and existing.deleted_at is None
        )
        try:
            perms: list[DrivePermission] | None = client.list_permissions(f.id)
        except PermissionsUnavailable:
            perms = None
        for email in group_emails(perms or []):
            if email not in groups:  # one lookup per group per run
                try:
                    groups[email] = client.list_group_members(email)
                except DriveError:
                    groups[email] = None
        need_content = supported_type(f) and (
            not live_doc or existing.content_fingerprint != f.content_fingerprint
        )
        data: bytes | None = None
        media_type = ""
        filename = f.name
        content_error: str | None = None
        if need_content:
            try:
                data, filename = client.download(f)
                detected = detect(filename, data)
            except ContentUnavailable as exc:
                content_error, data = exc.code, None
            except ParseFailed as exc:
                content_error, data = exc.code, None
            else:
                media_type = detected.media_type
        jobs: list[UUID] = []
        with tenant_transaction(self._engine, source.tenant_id, requested_by) as conn:
            self._fence(conn, source, outcome)  # nothing below runs for a stale attempt
            resolved = resolve_permissions(conn, perms, groups) if perms is not None else None
            perm_fp = _fingerprint(source, resolved) if resolved else None
            unresolved = json.dumps(list(resolved.unresolved) if resolved else [])
            document_id = existing.document_id if live_doc else None
            state = "active"
            if not supported_type(f) or (not live_doc and data is None):
                state = "unsupported"
                outcome.bump("unsupported")
            elif not live_doc and data is not None:
                document_id = uuid.uuid4()
                key = object_key(source.tenant_id, SOURCE, document_id, 1, filename)
                self._objects.put(key, data)
                perms_row = (
                    store.Permissions(
                        classification=source.classification,
                        department=source.department,
                        department_restricted=source.department_restricted,
                        allowed_countries=source.allowed_countries,
                        grants=resolved.grants,
                    )
                    if resolved
                    else None
                )
                _, job_id, _ = store.create_document(
                    conn,
                    tenant_id=source.tenant_id,
                    title=_title(f.name),
                    filename=filename[:255],
                    media_type=media_type,
                    byte_size=len(data),
                    byte_sha256=hashlib.sha256(data).hexdigest(),
                    storage_key_for=lambda d, v: key,
                    permissions=perms_row,
                    unknown_department=source.department,
                    created_by=requested_by,
                    source=SOURCE,
                    document_id=document_id,
                )
                conn.execute(
                    text("UPDATE resources SET source_id = :s WHERE id = :d"),
                    {"s": source.id, "d": document_id},
                )
                if resolved:
                    write_group_grants(conn, source.tenant_id, document_id, resolved.group_grants)
                jobs.append(job_id)
                outcome.bump("created")
            else:
                assert document_id is not None  # noqa: S101 - live_doc
                if data is not None:
                    version = int(existing.current_version) + 1
                    key = object_key(source.tenant_id, SOURCE, document_id, version, filename)
                    self._objects.put(key, data)
                    _, job_id, _ = store.add_version(
                        conn,
                        tenant_id=source.tenant_id,
                        document_id=document_id,
                        filename=filename[:255],
                        media_type=media_type,
                        byte_size=len(data),
                        byte_sha256=hashlib.sha256(data).hexdigest(),
                        storage_key_for=lambda d, v: key,
                        created_by=requested_by,
                    )
                    jobs.append(job_id)
                    outcome.bump("updated")
                elif content_error:
                    outcome.bump("content_errors")
                if resolved is None:
                    conn.execute(
                        text(
                            "UPDATE resources SET permission_state = 'unknown', updated_at = now()"
                            " WHERE id = :d AND permission_state <> 'unknown'"
                        ),
                        {"d": document_id},
                    )
                    write_group_grants(conn, source.tenant_id, document_id, ())
                elif perm_fp != existing.permissions_fingerprint or existing.state != "active":
                    store.set_permissions(
                        conn,
                        source.tenant_id,
                        document_id,
                        store.Permissions(
                            classification=source.classification,
                            department=source.department,
                            department_restricted=source.department_restricted,
                            allowed_countries=source.allowed_countries,
                            grants=resolved.grants,
                        ),
                    )
                    write_group_grants(conn, source.tenant_id, document_id, resolved.group_grants)
                    outcome.bump("permissions_changed")
                    record_audit_event(
                        conn,
                        request_id=None,
                        tenant_id=source.tenant_id,
                        user_id=requested_by,
                        event_type="connector.permissions",
                        action="resource.admin",
                        resource_id=document_id,
                        decision="info",
                        reason="drive_permissions_synced",
                        details={"source_id": str(source.id), "grants": len(resolved.grants)},
                    )
                if existing.name != f.name or existing.path != item.path:
                    title = _title(f.name)
                    conn.execute(
                        text(
                            "UPDATE documents SET title = :t, updated_at = clock_timestamp()"
                            " WHERE id = :d"
                        ),
                        {"t": title, "d": document_id},
                    )
                    conn.execute(
                        text("UPDATE resources SET title = :t, updated_at = now() WHERE id = :d"),
                        {"t": title, "d": document_id},
                    )
                    outcome.bump("moved_or_renamed")
            if resolved is None and state != "unsupported":
                state = "permissions_unknown"
                outcome.bump("permissions_unknown")
            if data is not None:
                content_fp: str | None = f.content_fingerprint
            elif live_doc and content_error:
                content_fp = existing.content_fingerprint  # retry the download next time
            elif live_doc:
                content_fp = f.content_fingerprint
            else:
                content_fp = None
            params = {
                "t": source.tenant_id,
                "s": source.id,
                "e": f.id,
                "d": document_id,
                "n": f.name[:500],
                "m": f.mime_type[:200],
                "p": item.parent,
                "path": item.path[:2000],
                "cf": content_fp,
                "pf": perm_fp,
                "u": unresolved,
                "st": state,
                "run": outcome.run_id,
            }
            conn.execute(
                text(
                    """
                    INSERT INTO drive_items (tenant_id, source_id, external_id, document_id, name,
                        mime_type, parent_external_id, path, content_fingerprint,
                        permissions_fingerprint, unresolved_principals, state, last_seen_run)
                    VALUES (:t, :s, :e, :d, :n, :m, :p, :path, :cf, :pf, CAST(:u AS jsonb), :st,
                            :run)
                    ON CONFLICT (tenant_id, source_id, external_id) DO UPDATE SET
                        document_id = EXCLUDED.document_id, name = EXCLUDED.name,
                        mime_type = EXCLUDED.mime_type,
                        parent_external_id = EXCLUDED.parent_external_id, path = EXCLUDED.path,
                        content_fingerprint = EXCLUDED.content_fingerprint,
                        permissions_fingerprint = EXCLUDED.permissions_fingerprint,
                        unresolved_principals = EXCLUDED.unresolved_principals,
                        state = EXCLUDED.state, last_seen_run = EXCLUDED.last_seen_run,
                        removed_at = NULL, updated_at = clock_timestamp()
                    """
                ),
                params,
            )
            outcome.bump("seen")
        return jobs

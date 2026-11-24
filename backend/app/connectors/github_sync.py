"""GitHub repository sync (M5, ADR 0502, FR-10).

A source is one repository and one branch, bound to one tenant and one owning
department. One run:

1. checks the repository against the operator allowlist (before any GitHub request);
2. reads the repository (visibility, default branch) - 401/403/404 fail the run;
3. lists **every** read-capable collaborator (``affiliation=all`` covers outside
   collaborators, org members and teams) - failure fails the run;
4. resolves the branch head commit and reads the full recursive tree - a truncated
   tree fails the run (the listing would not be complete);
5. per path: ingests changed text/code files through the M3 path (permissions before
   any job; immutable versions carrying ``repository/commit/path/blob``); records
   excluded, oversized, symlinked and submodule entries without ingesting them;
6. tombstones paths that disappeared (deleted or renamed files) and paths that became
   excluded; Qdrant points are removed;
7. in the final fenced transaction: replaces ``github_collaborators``, applies the
   source's attributes to every live document, records visibility and the commit, and
   sets ``permissions_current`` with the run's start time as ``last_verified_sync_at``.

Authorization: a repository has one permission set. Department, classification,
restriction and countries come from the source (layers 1-3, 5); layer 4 is derived
at authorization time from ``github_collaborators`` joined with ``external_accounts``
(``app.repositories.resources``), so unlinking a login applies at once and a removed
collaborator loses access at the next verified sync. Repository visibility is recorded
and audited but never widens access: a public repository still grants only its
linked collaborators.

Deletion and revocation: a repository that is deleted, renamed, transferred or no
longer visible to the token fails the run, so ``permissions_current`` becomes false and
all its content is denied at once; it stays denied (and is denied by the staleness
bound even if the scheduler stops). Deleting the source in the admin console
tombstones all its documents.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.connectors import paths
from app.connectors.fencing import FencedSyncBase, SourceRow, SyncOutcome, SyncSuperseded
from app.connectors.github import (
    MAX_BYTES,
    Collaborator,
    GitHubClient,
    GitHubError,
    GitHubNotConfigured,
    Repository,
    RepositoryMoved,
    RepositoryNotAllowed,
    TreeEntry,
)
from app.db.engine import tenant_transaction
from app.documents import store
from app.documents.parsers import VERBATIM_EXTENSIONS, ParseFailed, detect
from app.documents.storage import object_key
from app.repositories.audit import record_audit_event

logger = logging.getLogger("eaios.connectors.github")
SOURCE = "github"
MAX_ITEMS = 5000


def classify(entry: TreeEntry) -> tuple[str, str | None]:
    """(state, reason) for one tree entry; only ``active`` entries are ingested."""

    if entry.type == "commit":
        return "unsupported", "submodule"
    if entry.mode == "120000":
        return "symlink", "symlink_not_followed"
    reason = paths.exclusion(entry.path)
    if reason is not None:
        return "excluded", reason
    if paths.extension(entry.path) not in VERBATIM_EXTENSIONS:
        return "unsupported", "file_type"
    if entry.size is not None and entry.size > MAX_BYTES:
        return "too_large", "over_10_mb"
    return "active", None


class GitHubSyncService(FencedSyncBase):
    ITEMS_TABLE = "connector_items"
    ITEM_KEY = "path"
    LOG_PREFIX = "github_sync"

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
        dropped: list[UUID] = []
        try:
            name = source.external_root
            if name.lower() not in self._roots(slug):  # re-read: never reaches GitHub
                raise RepositoryNotAllowed("repository removed from the operator allowlist")
            client: GitHubClient | None = self._clients(slug)
            if client is None:
                raise GitHubNotConfigured("no credentials for tenant")
            repo = client.get_repository(name)
            if repo.full_name.lower() != name.lower():
                raise RepositoryMoved("repository renamed or transferred")
            collaborators = client.list_collaborators(name)
            ref = source.external_ref or repo.default_branch
            commit = client.resolve_commit(name, ref)
            entries = [e for e in client.get_tree(name, commit) if e.type != "tree"]
            if len(entries) > MAX_ITEMS:
                raise GitHubError("too_many_items")
            outcome.context.update(
                commit=commit, ref=ref, repository=repo, collaborators=collaborators
            )
            seen = set()
            for entry in sorted(entries, key=lambda e: e.path):
                try:
                    path = paths.check_relative(entry.path)
                except paths.InvalidPath:
                    outcome.bump("invalid_path")
                    continue
                seen.add(path)
                jobs.extend(
                    self._apply(client, source, repo, ref, commit, entry, path, outcome, dropped)
                )
            outcome.status = "succeeded"
        except SyncSuperseded:
            self._superseded(source, outcome)
            return outcome
        except GitHubError as exc:
            outcome.status, outcome.error_code = "failed", exc.code
            logger.warning("github_sync_failed", extra={"code": exc.code})
        except Exception:
            outcome.status, outcome.error_code = "failed", "sync_internal_error"
            logger.exception("github_sync_internal_error")
        self._finish(source, outcome, requested_by, seen if outcome.status == "succeeded" else None)
        for document_id in dropped:
            self._drop_points(document_id)
        if self._dispatch is not None and outcome.status != "superseded":
            for job_id in jobs:
                self._dispatch(job_id, tenant_id)
        return outcome

    # -- per path --------------------------------------------------------------------------

    def _apply(
        self,
        client: GitHubClient,
        source: SourceRow,
        repo: Repository,
        ref: str,
        commit: str,
        entry: TreeEntry,
        path: str,
        outcome: SyncOutcome,
        dropped: list[UUID],
    ) -> list[UUID]:
        state, reason = classify(entry)
        with tenant_transaction(self._engine, source.tenant_id) as conn:
            existing = conn.execute(
                text(
                    "SELECT i.document_id, i.external_sha, i.state, d.deleted_at,"
                    " d.current_version FROM connector_items i LEFT JOIN documents d"
                    "   ON d.tenant_id = i.tenant_id AND d.id = i.document_id"
                    " WHERE i.source_id = :s AND i.path = :p"
                ),
                {"s": source.id, "p": path},
            ).one_or_none()
        live = (
            existing is not None
            and existing.document_id is not None
            and existing.deleted_at is None
        )
        live_doc: Any = existing  # narrowed: only dereferenced when ``live``
        data: bytes | None = None
        media_type = ""
        filename = path.rsplit("/", 1)[-1][:255]
        if state == "active" and not (live and live_doc.external_sha == entry.sha):
            data = client.get_blob(repo.full_name, entry.sha)  # failure fails the run
            if len(data) > MAX_BYTES:
                state, reason, data = "too_large", "over_10_mb", None
            else:
                try:
                    media_type = detect(filename, data).media_type
                except ParseFailed:
                    state, reason, data = "unsupported", "file_type", None
        jobs: list[UUID] = []
        with tenant_transaction(self._engine, source.tenant_id) as conn:
            self._fence(conn, source, outcome)
            document_id = live_doc.document_id if existing is not None else None
            if state != "active":
                if live:
                    self._tombstone(conn, live_doc.document_id)
                    dropped.append(live_doc.document_id)
                    outcome.bump("removed")
                outcome.bump(state)
            elif data is not None:
                revision = {
                    "repository": repo.full_name,
                    "ref": ref,
                    "commit": commit,
                    "path": path,
                    "blob_sha": entry.sha,
                }
                sha256 = hashlib.sha256(data).hexdigest()
                if live:
                    version = int(live_doc.current_version) + 1
                    key = object_key(
                        source.tenant_id, SOURCE, live_doc.document_id, version, filename
                    )
                    self._objects.put(key, data)
                    _, job_id, _ = store.add_version(
                        conn,
                        tenant_id=source.tenant_id,
                        document_id=live_doc.document_id,
                        filename=filename,
                        media_type=media_type,
                        byte_size=len(data),
                        byte_sha256=sha256,
                        storage_key_for=lambda d, v: key,
                        created_by=None,
                        source_revision=revision,
                    )
                    outcome.bump("updated")
                else:
                    document_id = uuid.uuid4()
                    key = object_key(source.tenant_id, SOURCE, document_id, 1, filename)
                    self._objects.put(key, data)
                    _, job_id, _ = store.create_document(
                        conn,
                        tenant_id=source.tenant_id,
                        title=path[:300],
                        filename=filename,
                        media_type=media_type,
                        byte_size=len(data),
                        byte_sha256=sha256,
                        storage_key_for=lambda d, v: key,
                        permissions=store.Permissions(
                            classification=source.classification,
                            department=source.department,
                            department_restricted=source.department_restricted,
                            allowed_countries=source.allowed_countries,
                            grants=(),  # layer 4 is derived from collaborators
                        ),
                        unknown_department=source.department,
                        created_by=None,
                        source=SOURCE,
                        document_id=document_id,
                        source_revision=revision,
                    )
                    conn.execute(
                        text("UPDATE resources SET source_id = :s WHERE id = :d"),
                        {"s": source.id, "d": document_id},
                    )
                    outcome.bump("created")
                jobs.append(job_id)
            else:
                outcome.bump("unchanged")
            conn.execute(
                text(
                    """
                    INSERT INTO connector_items (tenant_id, source_id, path, document_id, size,
                        content_sha256, external_sha, revision, state, reason, last_seen_run)
                    VALUES (:t, :s, :p, :d, :size, :csha, :esha, :rev, :st, :reason, :run)
                    ON CONFLICT (tenant_id, source_id, path) DO UPDATE SET
                        document_id = EXCLUDED.document_id, size = EXCLUDED.size,
                        content_sha256 = coalesce(EXCLUDED.content_sha256,
                                                  connector_items.content_sha256),
                        external_sha = EXCLUDED.external_sha,
                        revision = CASE WHEN EXCLUDED.content_sha256 IS NULL
                                        AND EXCLUDED.state = 'active'
                                        THEN connector_items.revision ELSE EXCLUDED.revision END,
                        state = EXCLUDED.state, reason = EXCLUDED.reason,
                        last_seen_run = EXCLUDED.last_seen_run,
                        removed_at = NULL, updated_at = clock_timestamp()
                    """
                ),
                {
                    "t": source.tenant_id,
                    "s": source.id,
                    "p": path,
                    "d": document_id,
                    "size": entry.size,
                    "csha": hashlib.sha256(data).hexdigest() if data is not None else None,
                    "esha": entry.sha if len(entry.sha) == 40 else None,
                    "rev": commit,
                    "st": state,
                    "reason": reason,
                    "run": outcome.run_id,
                },
            )
            outcome.bump("seen")
        return jobs

    # -- final fenced transaction -------------------------------------------------------------

    def _finish_extra(self, conn: Connection, source: SourceRow, outcome: SyncOutcome) -> None:
        repo: Repository = outcome.context["repository"]
        collaborators: list[Collaborator] = outcome.context["collaborators"]
        wanted = {c.login: c.permission for c in collaborators if c.can_read}
        current = {
            r.login: r.permission
            for r in conn.execute(
                text("SELECT login, permission FROM github_collaborators WHERE source_id = :s"),
                {"s": source.id},
            ).all()
        }
        removed = sorted(set(current) - set(wanted))
        if removed:
            conn.execute(
                text("DELETE FROM github_collaborators WHERE source_id = :s AND login = ANY(:l)"),
                {"s": source.id, "l": removed},
            )
        for login, permission in sorted(wanted.items()):
            if login not in current:
                conn.execute(
                    text(
                        "INSERT INTO github_collaborators (tenant_id, source_id, login, permission)"
                        " VALUES (:t, :s, :l, :p)"
                    ),
                    {"t": source.tenant_id, "s": source.id, "l": login, "p": permission},
                )
            elif current[login] != permission:
                conn.execute(
                    text(
                        "UPDATE github_collaborators SET permission = :p"
                        " WHERE source_id = :s AND login = :l"
                    ),
                    {"s": source.id, "l": login, "p": permission},
                )
        linked = {
            r.login
            for r in conn.execute(
                text("SELECT login FROM external_accounts WHERE provider = 'github'")
            ).all()
        }
        unresolved = [
            {"type": "github_user", "login": login, "reason": "no_linked_user"}
            for login in sorted(wanted)
            if login not in linked
        ]
        previous = conn.execute(
            text("SELECT visibility FROM connector_sources WHERE id = :s"), {"s": source.id}
        ).scalar_one_or_none()
        conn.execute(
            text(
                "UPDATE connector_sources SET visibility = :v, external_revision = :c,"
                " unresolved_principals = CAST(:u AS jsonb) WHERE id = :s"
            ),
            {
                "v": repo.visibility
                if repo.visibility in ("private", "internal", "public")
                else None,
                "c": outcome.context["commit"],
                "u": json.dumps(unresolved),
                "s": source.id,
            },
        )
        # Source attributes (layers 1-3, 5) on every live document; only real changes
        # write (and bump the policy epoch through the resources trigger).
        changed = conn.execute(
            text(
                """
                UPDATE resources SET classification = :cls, department = :dept,
                       department_restricted = :restricted, allowed_countries = :countries,
                       permission_state = 'verified', updated_at = now()
                 WHERE source_id = :s AND status = 'active'
                   AND (classification, department, department_restricted, allowed_countries,
                        permission_state)
                       IS DISTINCT FROM (:cls, :dept, :restricted, :countries, 'verified')
                """
            ),
            {
                "cls": source.classification,
                "dept": source.department,
                "restricted": source.department_restricted,
                "countries": list(source.allowed_countries),
                "s": source.id,
            },
        ).rowcount
        outcome.stats.update(
            collaborators=len(wanted),
            collaborators_removed=len(removed),
            unlinked_collaborators=len(unresolved),
        )
        if changed:
            outcome.bump("permissions_changed", int(changed))
        if previous is not None and previous != repo.visibility:
            record_audit_event(
                conn,
                request_id=None,
                tenant_id=source.tenant_id,
                event_type="connector.visibility",
                action="resource.admin",
                decision="info",
                reason="repository_visibility_changed",
                details={
                    "source_id": str(source.id),
                    "from": previous,
                    "to": repo.visibility,
                    "department": source.department,
                },
            )

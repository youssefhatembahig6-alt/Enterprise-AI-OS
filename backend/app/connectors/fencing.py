"""Fenced connector sync runs (M4 ADR 0401 §9-10, reused by M5 ADR 0502).

A run holds its source through ``claim_run_id`` + ``claim_token`` and remembers the
source's ``policy_version``. Every transaction that writes on the run's behalf first
calls ``_fence``; an admin policy change or a newer worker that took over an expired
lease makes the older attempt ``superseded`` so it can no longer write items, grants,
tombstones, ``permissions_current`` or release the newer claim. Redelivered run ids
are recognised (duplicate, recovery with ``attempt + 1``, or superseded).

Subclasses provide the connector walk (``sync``) and name their item table.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text

from app.config import Settings
from app.db.engine import tenant_transaction
from app.documents import store
from app.documents.ingest import collection_name
from app.documents.qdrant import QdrantClient, QdrantError
from app.documents.storage import ObjectStore
from app.reporting.model_client import ModelClient
from app.repositories.audit import record_audit_event
from app.tenancy.config import CapabilityDisabled, require_connector

logger = logging.getLogger("eaios.connectors.sync")


@dataclass(frozen=True, slots=True)
class SourceRow:
    id: UUID
    tenant_id: UUID
    kind: str
    name: str
    external_root: str
    department: str
    classification: str
    department_restricted: bool
    allowed_countries: tuple[str, ...]
    status: str
    policy_version: int = 1
    external_ref: str | None = None


class SyncSuperseded(Exception):
    """This run no longer holds the source (policy changed or lease taken over)."""


@dataclass
class SyncOutcome:
    status: str  # running | succeeded | failed | skipped | busy | superseded
    run_id: UUID | None = None
    error_code: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    token: UUID | None = None  # this attempt's claim token (fencing)
    attempt: int = 1
    duplicate: bool = False  # a redelivery of a run that exists; no work was done
    # Connector-specific values a run carries to its final transaction (not persisted).
    context: dict[str, Any] = field(default_factory=dict)

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n


def _source(row: Any) -> SourceRow:
    return SourceRow(
        id=row.id,
        tenant_id=row.tenant_id,
        kind=row.kind,
        name=row.name,
        external_root=row.external_root,
        department=row.department,
        classification=row.classification,
        department_restricted=row.department_restricted,
        allowed_countries=tuple(row.allowed_countries),
        status=row.status,
        policy_version=int(row.policy_version),
        external_ref=row.external_ref,
    )


_SOURCE_SELECT = (
    "SELECT id, tenant_id, kind, name, external_root, department, classification,"
    " department_restricted, allowed_countries, status, policy_version, external_ref"
    " FROM connector_sources WHERE id = :id"
)
_SOURCE_LOCK = _SOURCE_SELECT + " FOR UPDATE"


def load_source(conn: Connection, source_id: UUID, *, lock: bool = False) -> SourceRow | None:
    row = conn.execute(
        text(_SOURCE_LOCK if lock else _SOURCE_SELECT),
        {"id": source_id},
    ).one_or_none()
    return _source(row) if row else None


class FencedSyncBase:
    """Claim, fence, supersede and finish a sync run. ``ITEMS_TABLE`` / ``ITEM_KEY`` name
    the per-source item table and its key column (tombstoning unseen items)."""

    ITEMS_TABLE = "drive_items"
    ITEM_KEY = "external_id"
    LOG_PREFIX = "drive_sync"

    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        objects: ObjectStore,
        qdrant: QdrantClient,
        model: ModelClient,
        clients: Any,
        dispatch: Callable[[UUID, UUID], None] | None,
        lease_seconds: int = 900,
        roots: Callable[[str], frozenset[str]] | None = None,
    ) -> None:
        self._roots = roots or (lambda slug: frozenset())
        self._engine = engine
        self._settings = settings
        self._objects = objects
        self._qdrant = qdrant
        self._model = model
        self._clients = clients
        self._dispatch = dispatch
        self._lease = lease_seconds

    def _finish_extra(self, conn: Connection, source: SourceRow, outcome: SyncOutcome) -> None:
        """Connector-specific writes in the final fenced transaction (success only)."""

    # -- claim / fence / finish ------------------------------------------------------------

    @staticmethod
    def _fence(conn: Connection, source: SourceRow, outcome: SyncOutcome) -> None:
        """Lock the source row and prove this *attempt* still holds it under the policy
        it started with. Called first in every transaction that writes on its behalf."""

        held = conn.execute(
            text(
                "SELECT 1 FROM connector_sources WHERE id = :s AND claim_token = :tok"
                " AND claim_run_id = :r AND policy_version = :pv AND status = 'active'"
                " FOR UPDATE"
            ),
            {
                "s": source.id,
                "tok": outcome.token,
                "r": outcome.run_id,
                "pv": source.policy_version,
            },
        ).scalar()
        if held is None:
            raise SyncSuperseded

    def _claim(
        self,
        tenant_id: UUID,
        source_id: UUID,
        trigger: str,
        requested_by: UUID | None,
        run_id: UUID | None,
    ) -> tuple[SyncOutcome, SourceRow | None, str]:
        """Claim the source for a run. A manual run id may be delivered more than once
        (Celery redelivery): an existing run is recognised, never re-inserted.

        * terminal run            -> duplicate, nothing done, its status reported
        * running, live lease     -> duplicate, nothing done (the first delivery works)
        * running, lease expired  -> recovery: same run, attempt + 1, new claim token;
                                     the dead attempt is fenced out
        * running, claim moved on -> the run is ``superseded`` (truthful terminal state)
        * a different run claims   -> every other run still ``running`` for the source is
                                     closed as ``superseded`` (``claim_taken_over``)
        * queued / new            -> normal claim (or ``skipped`` with a reason)
        """

        run_id = run_id or uuid.uuid4()
        token = uuid.uuid4()
        with tenant_transaction(self._engine, tenant_id, requested_by) as conn:
            source = load_source(conn, source_id, lock=True)
            existing = conn.execute(
                text(
                    "SELECT status, attempt FROM connector_sync_runs"
                    " WHERE id = :id AND source_id = :s FOR UPDATE"
                ),
                {"id": run_id, "s": source_id},
            ).one_or_none()
            attempt = 1
            if existing is not None and existing.status != "queued":
                if existing.status != "running":
                    out = SyncOutcome(existing.status, run_id=run_id, duplicate=True)
                    out.attempt = int(existing.attempt)
                    return out, None, ""
                claim = conn.execute(
                    text(
                        "SELECT claim_run_id, sync_claimed_until > clock_timestamp() AS live"
                        " FROM connector_sources WHERE id = :s"
                    ),
                    {"s": source_id},
                ).one()
                if claim.claim_run_id == run_id and claim.live:
                    out = SyncOutcome("running", run_id=run_id, duplicate=True)
                    out.attempt = int(existing.attempt)
                    return out, None, ""
                if claim.claim_run_id not in (run_id, None) and claim.live:
                    conn.execute(
                        text(
                            "UPDATE connector_sync_runs SET status = 'superseded',"
                            " error_code = 'sync_superseded', finished_at = clock_timestamp()"
                            " WHERE id = :id AND status = 'running'"
                        ),
                        {"id": run_id},
                    )
                    out = SyncOutcome("superseded", run_id=run_id, duplicate=True)
                    out.error_code = "sync_superseded"
                    return out, None, ""
                attempt = int(existing.attempt) + 1  # recover the expired attempt
            skip: str | None = None
            if source is None or source.status != "active":
                skip = "source_inactive"
            else:
                try:
                    require_connector(conn, source.kind)
                except CapabilityDisabled:
                    skip = "capability_disabled"
            if skip is None and attempt == 1:
                busy = conn.execute(
                    text(
                        "SELECT sync_claimed_until > clock_timestamp() FROM connector_sources"
                        " WHERE id = :id"
                    ),
                    {"id": source_id},
                ).scalar()
                skip = "sync_in_progress" if busy else None
            status = "skipped" if skip else "running"
            pv = source.policy_version if source else None
            if existing is not None:
                conn.execute(
                    text(
                        "UPDATE connector_sync_runs SET status = :st, error_code = :code,"
                        " started_at = clock_timestamp(), policy_version = :pv, attempt = :a,"
                        " finished_at = CASE WHEN :st = 'skipped' THEN clock_timestamp() END"
                        " WHERE id = :id"
                    ),
                    {"st": status, "code": skip, "pv": pv, "a": attempt, "id": run_id},
                )
            elif source is not None:
                conn.execute(
                    text(
                        "INSERT INTO connector_sync_runs (id, tenant_id, source_id, trigger,"
                        " requested_by, status, error_code, policy_version, finished_at)"
                        " VALUES (:id, :t, :s, :tr, :u, :st, :code, :pv,"
                        "         CASE WHEN :st = 'skipped' THEN clock_timestamp() END)"
                    ),
                    {
                        "id": run_id,
                        "t": tenant_id,
                        "s": source_id,
                        "tr": trigger,
                        "u": requested_by,
                        "st": status,
                        "code": skip,
                        "pv": pv,
                    },
                )
            if skip is not None:
                outcome = SyncOutcome("busy" if skip == "sync_in_progress" else "skipped")
                outcome.run_id, outcome.error_code = run_id, skip
                return outcome, None, ""
            # Taking over: any *other* run of this source still marked running lost its
            # claim (its worker died or it was fenced out) and is closed as superseded
            # now, so it never stays "running" forever. A later delivery of it sees a
            # terminal run and does nothing. Recovery of this same run id is excluded.
            conn.execute(
                text(
                    "UPDATE connector_sync_runs SET status = 'superseded',"
                    " error_code = 'claim_taken_over', finished_at = clock_timestamp()"
                    " WHERE source_id = :s AND status = 'running' AND id <> :run"
                ),
                {"s": source_id, "run": run_id},
            )
            conn.execute(
                text(
                    """
                    UPDATE connector_sources SET last_sync_started_at = clock_timestamp(),
                           last_sync_status = 'running', claim_run_id = :run,
                           claim_token = :tok,
                           sync_claimed_until = clock_timestamp() + make_interval(secs => :lease),
                           updated_at = now()
                     WHERE id = :id
                    """
                ),
                {"id": source_id, "lease": self._lease, "run": run_id, "tok": token},
            )
            slug = str(conn.execute(text("SELECT slug FROM tenants")).scalar_one())
        outcome = SyncOutcome("running", run_id=run_id, token=token, attempt=attempt)
        return outcome, source, slug

    def _superseded(self, source: SourceRow, outcome: SyncOutcome) -> None:
        """Record on the run's own row, and only for this attempt: a recovered attempt
        of the same run id is never overwritten, and the source is left alone."""

        outcome.status, outcome.error_code = "superseded", "sync_superseded"
        with tenant_transaction(self._engine, source.tenant_id) as conn:
            conn.execute(
                text(
                    "UPDATE connector_sync_runs SET status = 'superseded',"
                    " error_code = 'sync_superseded', finished_at = clock_timestamp(),"
                    " stats = CAST(:stats AS jsonb)"
                    " WHERE id = :id AND attempt = :a AND status = 'running'"
                ),
                {
                    "id": outcome.run_id,
                    "a": outcome.attempt,
                    "stats": json.dumps(outcome.stats, sort_keys=True),
                },
            )
            record_audit_event(
                conn,
                request_id=None,
                tenant_id=source.tenant_id,
                event_type="connector.sync",
                action="connector.sync",
                decision="deny",
                reason="sync_superseded",
                details={
                    "source_id": str(source.id),
                    "run_id": str(outcome.run_id),
                    "attempt": outcome.attempt,
                    "department": source.department,
                },
            )
        logger.warning(f"{self.LOG_PREFIX}_superseded")

    def _finish(
        self,
        source: SourceRow,
        outcome: SyncOutcome,
        requested_by: UUID | None,
        seen_ids: set[str] | None,
    ) -> None:
        """Tombstone unseen items (success only) and record the verified sync time, in
        one fenced transaction.

        ``last_verified_sync_at`` becomes the run's *start* time: every permission was
        read after it, so it is a conservative lower bound."""

        removed_docs: list[UUID] = []
        try:
            with tenant_transaction(self._engine, source.tenant_id, requested_by) as conn:
                self._fence(conn, source, outcome)
                if outcome.status == "succeeded" and seen_ids is not None:
                    rows = conn.execute(
                        text(
                            f"SELECT {self.ITEM_KEY} AS item_key, document_id"  # noqa: S608
                            f" FROM {self.ITEMS_TABLE}"
                            " WHERE source_id = :s AND state <> 'removed'"
                        ),
                        {"s": source.id},
                    ).all()
                    for row in rows:
                        if row.item_key in seen_ids:
                            continue
                        if row.document_id is not None:
                            self._tombstone(conn, row.document_id)
                            removed_docs.append(row.document_id)
                        conn.execute(
                            text(
                                f"UPDATE {self.ITEMS_TABLE} SET state = 'removed',"  # noqa: S608
                                " removed_at = clock_timestamp(),"
                                " updated_at = clock_timestamp()"
                                f" WHERE source_id = :s AND {self.ITEM_KEY} = :e"
                            ),
                            {"s": source.id, "e": row.item_key},
                        )
                        outcome.bump("removed")
                ok = outcome.status == "succeeded"
                if ok:
                    self._finish_extra(conn, source, outcome)
                conn.execute(
                    text(
                        "UPDATE connector_sync_runs SET status = :st,"
                        " finished_at = clock_timestamp(), error_code = :code,"
                        " stats = CAST(:stats AS jsonb) WHERE id = :id"
                    ),
                    {
                        "st": outcome.status,
                        "code": outcome.error_code,
                        "stats": json.dumps(outcome.stats, sort_keys=True),
                        "id": outcome.run_id,
                    },
                )
                conn.execute(
                    text(
                        """
                        UPDATE connector_sources s SET
                               permissions_current = :ok,
                               last_verified_sync_at = CASE WHEN :ok THEN r.started_at
                                                            ELSE s.last_verified_sync_at END,
                               last_sync_status = :st, last_error_code = :code,
                               last_sync_finished_at = clock_timestamp(),
                               sync_claimed_until = NULL, claim_run_id = NULL,
                               claim_token = NULL,
                               updated_at = now()
                          FROM connector_sync_runs r
                         WHERE s.id = :sid AND r.id = :rid AND s.claim_run_id = :rid
                           AND s.claim_token = :tok
                        """
                    ),
                    {
                        "ok": ok,
                        "st": outcome.status,
                        "code": outcome.error_code,
                        "sid": source.id,
                        "rid": outcome.run_id,
                        "tok": outcome.token,
                    },
                )
                record_audit_event(
                    conn,
                    request_id=None,
                    tenant_id=source.tenant_id,
                    user_id=requested_by,
                    event_type="connector.sync",
                    action="connector.sync",
                    decision="info" if ok else "deny",
                    reason="sync_succeeded" if ok else f"sync_failed:{outcome.error_code}",
                    details={
                        "source_id": str(source.id),
                        "run_id": str(outcome.run_id),
                        "stats": outcome.stats,
                        "department": source.department,
                    },
                )
        except SyncSuperseded:
            self._superseded(source, outcome)
            return
        for document_id in removed_docs:
            self._drop_points(document_id)

    # -- removal -----------------------------------------------------------------------------

    @staticmethod
    def _tombstone(conn: Connection, document_id: UUID) -> None:
        doc = store.load_document(conn, document_id)
        if doc is not None and doc.deleted_at is None:
            store.delete_document(conn, document_id)

    def _drop_points(self, document_id: UUID) -> None:
        try:  # eligibility already excludes the document; this only frees space
            self._qdrant.delete_points(
                collection_name(self._settings, self._model),
                {"must": [{"key": "document_id", "match": {"value": str(document_id)}}]},
            )
        except QdrantError:
            logger.warning("qdrant_delete_failed", extra={"document_id": str(document_id)})

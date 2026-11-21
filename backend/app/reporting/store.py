"""Run, event, evidence, profile and job persistence (application role, tenant RLS).

Every function takes a connection opened with ``tenant_transaction`` so row-level
security limits it to the trusted tenant. Ownership is additionally checked in SQL.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.reporting.contracts import EVENT_VERSION, EventType, ReportProfile, RunStatus
from app.reporting.executor import QueryResult, result_sha256


@dataclass(frozen=True, slots=True)
class RunRow:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    resource_id: UUID | None
    report_key: str | None
    question: str
    status: RunStatus
    cancel_requested: bool
    inference_mode: str
    policy_epoch_at_start: int
    outcome_code: str | None
    result: dict[str, Any] | None
    stats: dict[str, Any]
    last_seq: int
    created_at: datetime
    finished_at: datetime | None
    kind: str = "report"


_RUN_COLUMNS = """id, tenant_id, owner_user_id, resource_id, report_key, question, status,
    cancel_requested, inference_mode, policy_epoch_at_start, outcome_code, result, stats,
    last_seq, created_at, finished_at, kind"""


def _run(row: Any) -> RunRow:
    return RunRow(
        id=row.id,
        tenant_id=row.tenant_id,
        owner_user_id=row.owner_user_id,
        resource_id=row.resource_id,
        report_key=row.report_key,
        question=row.question,
        status=RunStatus(row.status),
        cancel_requested=row.cancel_requested,
        inference_mode=row.inference_mode,
        policy_epoch_at_start=row.policy_epoch_at_start,
        outcome_code=row.outcome_code,
        result=row.result,
        stats=row.stats or {},
        last_seq=row.last_seq,
        created_at=row.created_at,
        finished_at=row.finished_at,
        kind=row.kind,
    )


def load_profile(conn: Connection, report_key: str) -> ReportProfile | None:
    row = conn.execute(
        text(
            "SELECT report_key, resource_id, version, config FROM tenant_report_profiles"
            " WHERE report_key = :key"
        ),
        {"key": report_key},
    ).one_or_none()
    if row is None:
        return None
    return ReportProfile.from_row(row.report_key, row.resource_id, row.version, row.config)


def load_profiles(conn: Connection) -> list[ReportProfile]:
    rows = conn.execute(
        text(
            "SELECT report_key, resource_id, version, config FROM tenant_report_profiles"
            " ORDER BY report_key"
        )
    ).all()
    return [ReportProfile.from_row(r.report_key, r.resource_id, r.version, r.config) for r in rows]


def dataset_info(conn: Connection) -> dict[str, Any] | None:
    """The tenant's imported business dataset, if any (fingerprint and coverage)."""

    row = conn.execute(
        text("SELECT dataset, normalized_fingerprint, reference_date FROM business_dataset_imports")
    ).one_or_none()
    if row is None:
        return None
    return {
        "dataset": row.dataset,
        "fingerprint": row.normalized_fingerprint,
        "reference_date": row.reference_date,
    }


def create_run(
    conn: Connection,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    question: str,
    client_request_id: str,
    inference_mode: str,
    policy_epoch: int,
    kind: str = "report",
) -> tuple[RunRow, bool]:
    """Insert the run and its job row in the caller's transaction. Idempotent on
    ``(tenant, owner, client_request_id)``: a retried POST returns the same run."""

    run_id = uuid.uuid4()
    inserted = conn.execute(
        text(
            """
            INSERT INTO report_runs (id, tenant_id, owner_user_id, question, client_request_id,
                                     status, inference_mode, policy_epoch_at_start, kind)
            VALUES (:id, :t, :u, :q, :crid, 'queued', :mode, :epoch, :kind)
            ON CONFLICT (tenant_id, owner_user_id, client_request_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": run_id,
            "t": tenant_id,
            "u": owner_user_id,
            "q": question,
            "crid": client_request_id,
            "mode": inference_mode,
            "epoch": policy_epoch,
            "kind": kind,
        },
    ).one_or_none()
    if inserted is not None:
        conn.execute(
            text("INSERT INTO report_jobs (run_id, tenant_id, status) VALUES (:r, :t, 'pending')"),
            {"r": run_id, "t": tenant_id},
        )
    row = conn.execute(
        text(
            f"SELECT {_RUN_COLUMNS} FROM report_runs"  # noqa: S608 - constant column list
            " WHERE owner_user_id = :u AND client_request_id = :crid"
        ),
        {"u": owner_user_id, "crid": client_request_id},
    ).one()
    return _run(row), inserted is not None


def load_run(conn: Connection, run_id: UUID, owner_user_id: UUID | None = None) -> RunRow | None:
    sql = f"SELECT {_RUN_COLUMNS} FROM report_runs WHERE id = :id"  # noqa: S608
    params: dict[str, Any] = {"id": run_id}
    if owner_user_id is not None:
        sql += " AND owner_user_id = :u"
        params["u"] = owner_user_id
    row = conn.execute(text(sql), params).one_or_none()
    return _run(row) if row is not None else None


def lock_run(conn: Connection, run_id: UUID) -> RunRow | None:
    row = conn.execute(
        text(f"SELECT {_RUN_COLUMNS} FROM report_runs WHERE id = :id FOR UPDATE"),  # noqa: S608
        {"id": run_id},
    ).one_or_none()
    return _run(row) if row is not None else None


def append_event(
    conn: Connection, run_id: UUID, tenant_id: UUID, event: EventType, payload: dict[str, Any]
) -> int:
    seq = conn.execute(
        text(
            "UPDATE report_runs SET last_seq = last_seq + 1, updated_at = clock_timestamp()"
            " WHERE id = :id RETURNING last_seq"
        ),
        {"id": run_id},
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO report_run_events (tenant_id, run_id, seq, event_type, version, payload)"
            " VALUES (:t, :r, :seq, :type, :v, CAST(:payload AS jsonb))"
        ),
        {
            "t": tenant_id,
            "r": run_id,
            "seq": seq,
            "type": event.value,
            "v": EVENT_VERSION,
            "payload": json.dumps(payload, sort_keys=True),
        },
    )
    return int(seq)


@dataclass(frozen=True, slots=True)
class EventRow:
    seq: int
    event_type: EventType
    version: int
    payload: dict[str, Any]


def events_after(conn: Connection, run_id: UUID, after: int, limit: int = 200) -> list[EventRow]:
    rows = conn.execute(
        text(
            "SELECT seq, event_type, version, payload FROM report_run_events"
            " WHERE run_id = :r AND seq > :after ORDER BY seq LIMIT :limit"
        ),
        {"r": run_id, "after": after, "limit": limit},
    ).all()
    return [EventRow(r.seq, EventType(r.event_type), r.version, r.payload) for r in rows]


def update_run(conn: Connection, run_id: UUID, **fields: Any) -> None:
    allowed = {
        "status",
        "cancel_requested",
        "policy_epoch_at_publication",
        "outcome_code",
        "result",
        "stats",
        "finished_at",
        "resource_id",
        "report_key",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update {sorted(unknown)}")
    assignments = []
    params: dict[str, Any] = {"id": run_id}
    for name, value in fields.items():
        if name in ("result", "stats"):
            assignments.append(f"{name} = CAST(:{name} AS jsonb)")
            params[name] = None if value is None else json.dumps(value, sort_keys=True)
        elif name == "finished_at" and value == "now":
            assignments.append("finished_at = clock_timestamp()")
        else:
            assignments.append(f"{name} = :{name}")
            params[name] = value.value if isinstance(value, RunStatus) else value
    assignments.append("updated_at = clock_timestamp()")
    conn.execute(
        text(f"UPDATE report_runs SET {', '.join(assignments)} WHERE id = :id"),  # noqa: S608
        params,
    )


def request_cancel(conn: Connection, run_id: UUID, owner_user_id: UUID) -> RunRow | None:
    row = conn.execute(
        text(
            "UPDATE report_runs SET cancel_requested = true, updated_at = clock_timestamp()"
            " WHERE id = :id AND owner_user_id = :u RETURNING id"
        ),
        {"id": run_id, "u": owner_user_id},
    ).one_or_none()
    return load_run(conn, run_id, owner_user_id) if row is not None else None


def cancel_requested(conn: Connection, run_id: UUID) -> bool:
    value = conn.execute(
        text("SELECT cancel_requested FROM report_runs WHERE id = :id"), {"id": run_id}
    ).scalar_one_or_none()
    return bool(value)


# -- evidence ---------------------------------------------------------------------


def evidence_payload(
    *,
    evidence_id: UUID,
    tenant_id: UUID,
    run_id: UUID,
    result: QueryResult,
    dataset_fingerprint: str,
    policy_epoch: int,
) -> dict[str, Any]:
    return {
        "evidence_id": str(evidence_id),
        "tenant_id": str(tenant_id),
        "run_id": str(run_id),
        "executed_sql": result.executed_sql,
        "parameters": result.parameters,
        "columns": result.columns,
        "rows": result.rows,
        "dataset_fingerprint": dataset_fingerprint,
        "policy_epoch": policy_epoch,
        "executed_at": result.executed_at.isoformat(),
    }


def insert_evidence(
    conn: Connection,
    *,
    tenant_id: UUID,
    run_id: UUID,
    owner_user_id: UUID,
    resource_id: UUID,
    report_key: str,
    model_sql: str,
    result: QueryResult,
    semantics: dict[str, Any],
    dataset_fingerprint: str,
    policy_epoch: int,
) -> tuple[UUID, str]:
    evidence_id = uuid.uuid4()
    digest = result_sha256(
        evidence_payload(
            evidence_id=evidence_id,
            tenant_id=tenant_id,
            run_id=run_id,
            result=result,
            dataset_fingerprint=dataset_fingerprint,
            policy_epoch=policy_epoch,
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO report_evidence (id, tenant_id, run_id, owner_user_id, resource_id,
                report_key, model_sql, executed_sql, parameters, columns, rows, row_count,
                semantics, dataset_fingerprint, policy_epoch, executed_at, result_sha256)
            VALUES (:id, :t, :r, :u, :res, :key, :model_sql, :sql, CAST(:params AS jsonb),
                CAST(:columns AS jsonb), CAST(:rows AS jsonb), :n, CAST(:sem AS jsonb), :fp,
                :epoch, :at, :digest)
            """
        ),
        {
            "id": evidence_id,
            "t": tenant_id,
            "r": run_id,
            "u": owner_user_id,
            "res": resource_id,
            "key": report_key,
            "model_sql": model_sql,
            "sql": result.executed_sql,
            "params": json.dumps(result.parameters),
            "columns": json.dumps(result.columns),
            "rows": json.dumps(result.rows),
            "n": len(result.rows),
            "sem": json.dumps(semantics, sort_keys=True),
            "fp": dataset_fingerprint,
            "epoch": policy_epoch,
            "at": result.executed_at,
            "digest": digest,
        },
    )
    return evidence_id, digest


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    id: UUID
    tenant_id: UUID
    run_id: UUID
    owner_user_id: UUID
    resource_id: UUID
    report_key: str
    executed_sql: str
    parameters: dict[str, Any]
    columns: list[dict[str, str]]
    rows: list[list[Any]]
    row_count: int
    semantics: dict[str, Any]
    dataset_fingerprint: str
    policy_epoch: int
    executed_at: datetime
    result_sha256: str

    def recomputed_sha256(self) -> str:
        return result_sha256(
            {
                "evidence_id": str(self.id),
                "tenant_id": str(self.tenant_id),
                "run_id": str(self.run_id),
                "executed_sql": self.executed_sql,
                "parameters": self.parameters,
                "columns": self.columns,
                "rows": self.rows,
                "dataset_fingerprint": self.dataset_fingerprint,
                "policy_epoch": self.policy_epoch,
                "executed_at": self.executed_at.isoformat(),
            }
        )


def load_evidence(conn: Connection, evidence_id: UUID, owner_user_id: UUID) -> EvidenceRow | None:
    row = conn.execute(
        text(
            """
            SELECT id, tenant_id, run_id, owner_user_id, resource_id, report_key, executed_sql,
                   parameters, columns, rows, row_count, semantics, dataset_fingerprint,
                   policy_epoch, executed_at, result_sha256
              FROM report_evidence WHERE id = :id AND owner_user_id = :u
            """
        ),
        {"id": evidence_id, "u": owner_user_id},
    ).one_or_none()
    if row is None:
        return None
    return EvidenceRow(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        owner_user_id=row.owner_user_id,
        resource_id=row.resource_id,
        report_key=row.report_key,
        executed_sql=row.executed_sql,
        parameters=row.parameters,
        columns=row.columns,
        rows=row.rows,
        row_count=row.row_count,
        semantics=row.semantics,
        dataset_fingerprint=row.dataset_fingerprint,
        policy_epoch=row.policy_epoch,
        executed_at=row.executed_at,
        result_sha256=row.result_sha256,
    )


# -- jobs (outbox) ----------------------------------------------------------------


def claim_job(conn: Connection, run_id: UUID, lease_seconds: int) -> int | None:
    """Claim a pending or lease-expired job. Returns the attempt number, or None when
    another worker holds it or it is already done (duplicate deliveries are no-ops)."""

    value = conn.execute(
        text(
            """
            UPDATE report_jobs
               SET status = 'claimed', attempts = attempts + 1,
                   claimed_until = clock_timestamp() + make_interval(secs => :lease),
                   updated_at = clock_timestamp()
             WHERE run_id = :r
               AND (status = 'pending'
                    OR (status = 'claimed' AND claimed_until < clock_timestamp()))
            RETURNING attempts
            """
        ),
        {"r": run_id, "lease": lease_seconds},
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def finish_job(conn: Connection, run_id: UUID) -> None:
    conn.execute(
        text(
            "UPDATE report_jobs SET status = 'done', claimed_until = NULL,"
            " updated_at = clock_timestamp() WHERE run_id = :r"
        ),
        {"r": run_id},
    )


def mark_dispatched(conn: Connection, run_id: UUID) -> None:
    conn.execute(
        text(
            "UPDATE report_jobs SET last_dispatched_at = clock_timestamp(),"
            " updated_at = clock_timestamp() WHERE run_id = :r AND status = 'pending'"
        ),
        {"r": run_id},
    )

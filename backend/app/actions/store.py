"""SQL for M6 actions. Every function runs inside a tenant transaction (RLS)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


@dataclass(frozen=True, slots=True)
class UserRow:
    id: UUID
    email: str
    display_name: str
    active: bool


@dataclass(frozen=True, slots=True)
class ActionRow:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    source_run_id: UUID
    origin: str
    version: int
    status: str
    status_reason: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class VersionRow:
    action_id: UUID
    version: int
    recipient_user_id: UUID
    recipient_email: str
    subject: str
    body: str
    source_sha256: str
    draft_sha256: str
    edited_by: UUID | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    id: UUID
    action_id: UUID
    version: int
    draft_sha256: str
    actor_user_id: UUID
    session_id: UUID
    status: str
    expires_at: datetime
    expired: bool


@dataclass(frozen=True, slots=True)
class OutboxRow:
    execution_key: UUID
    tenant_id: UUID
    action_id: UUID
    version: int
    approval_id: UUID
    message_id: str
    status: str
    status_reason: str | None
    attempts: int
    reconcile_checks: int
    claimed_at: datetime | None
    smtp_accepted_at: datetime | None
    uncertain_since: datetime | None
    confirmed_at: datetime | None
    mailpit_id: str | None


def load_user(conn: Connection, user_id: UUID) -> UserRow | None:
    """Authoritative user record (RLS: another tenant's id is simply not found)."""

    row = conn.execute(
        text(
            "SELECT u.id, u.email, u.display_name, u.status = 'active' AND t.status = 'active'"
            " AS active FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE u.id = :id"
        ),
        {"id": user_id},
    ).one_or_none()
    return UserRow(row.id, row.email, row.display_name, row.active) if row else None


def list_active_users(conn: Connection) -> list[UserRow]:
    rows = conn.execute(
        text(
            "SELECT id, email, display_name FROM users WHERE status = 'active'"
            " ORDER BY display_name, email"
        )
    ).all()
    return [UserRow(r.id, r.email, r.display_name, True) for r in rows]


_ACTION = """id, tenant_id, owner_user_id, source_run_id, origin, version, status,
    status_reason, created_at, updated_at"""


def _action(row: Any) -> ActionRow:
    return ActionRow(
        row.id,
        row.tenant_id,
        row.owner_user_id,
        row.source_run_id,
        row.origin,
        row.version,
        row.status,
        row.status_reason,
        row.created_at,
        row.updated_at,
    )


def insert_action(
    conn: Connection, *, action_id: UUID, tenant_id: UUID, owner: UUID, run_id: UUID, origin: str
) -> None:
    conn.execute(
        text(
            "INSERT INTO email_actions (id, tenant_id, owner_user_id, source_run_id, origin)"
            " VALUES (:id, :t, :o, :r, :origin)"
        ),
        {"id": action_id, "t": tenant_id, "o": owner, "r": run_id, "origin": origin},
    )


def load_action(
    conn: Connection, action_id: UUID, owner: UUID | None = None, *, lock: bool = False
) -> ActionRow | None:
    sql = f"SELECT {_ACTION} FROM email_actions WHERE id = :id"  # noqa: S608
    params: dict[str, Any] = {"id": action_id}
    if owner is not None:
        sql += " AND owner_user_id = :o"
        params["o"] = owner
    if lock:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), params).one_or_none()
    return _action(row) if row else None


def list_actions(conn: Connection, owner: UUID, limit: int = 50) -> list[ActionRow]:
    rows = conn.execute(
        text(
            f"SELECT {_ACTION} FROM email_actions WHERE owner_user_id = :o"  # noqa: S608
            " ORDER BY created_at DESC LIMIT :n"
        ),
        {"o": owner, "n": limit},
    ).all()
    return [_action(r) for r in rows]


def set_action(
    conn: Connection,
    action_id: UUID,
    *,
    status: str,
    reason: str | None = None,
    version: int | None = None,
) -> None:
    conn.execute(
        text(
            "UPDATE email_actions SET status = :s, status_reason = :r,"
            " version = COALESCE(:v, version), updated_at = clock_timestamp() WHERE id = :id"
        ),
        {"s": status, "r": reason, "v": version, "id": action_id},
    )


def insert_version(conn: Connection, tenant_id: UUID, v: VersionRow) -> None:
    conn.execute(
        text(
            """
            INSERT INTO email_draft_versions (tenant_id, action_id, version, recipient_user_id,
                recipient_email, subject, body, source_sha256, draft_sha256, edited_by)
            VALUES (:t, :a, :v, :ru, :re, :s, :b, :src, :d, :e)
            """
        ),
        {
            "t": tenant_id,
            "a": v.action_id,
            "v": v.version,
            "ru": v.recipient_user_id,
            "re": v.recipient_email,
            "s": v.subject,
            "b": v.body,
            "src": v.source_sha256,
            "d": v.draft_sha256,
            "e": v.edited_by,
        },
    )


def load_version(conn: Connection, action_id: UUID, version: int) -> VersionRow | None:
    row = conn.execute(
        text(
            "SELECT action_id, version, recipient_user_id, recipient_email, subject, body,"
            " source_sha256, draft_sha256, edited_by, created_at FROM email_draft_versions"
            " WHERE action_id = :a AND version = :v"
        ),
        {"a": action_id, "v": version},
    ).one_or_none()
    if row is None:
        return None
    return VersionRow(
        row.action_id,
        row.version,
        row.recipient_user_id,
        row.recipient_email,
        row.subject,
        row.body,
        row.source_sha256,
        row.draft_sha256,
        row.edited_by,
        row.created_at,
    )


_APPROVAL = """id, action_id, version, draft_sha256, actor_user_id, session_id, status,
    expires_at, expires_at <= clock_timestamp() AS expired"""


def _approval(row: Any) -> ApprovalRow:
    return ApprovalRow(
        row.id,
        row.action_id,
        row.version,
        row.draft_sha256,
        row.actor_user_id,
        row.session_id,
        row.status,
        row.expires_at,
        row.expired,
    )


def invalidate_approvals(conn: Connection, action_id: UUID) -> int:
    return conn.execute(
        text(
            "UPDATE email_approvals SET status = 'invalidated'"
            " WHERE action_id = :a AND status = 'active'"
        ),
        {"a": action_id},
    ).rowcount


def insert_approval(
    conn: Connection,
    *,
    approval_id: UUID,
    tenant_id: UUID,
    action_id: UUID,
    version: int,
    draft_sha: str,
    actor: UUID,
    session_id: UUID,
    token_sha: str,
    ttl_seconds: int,
) -> datetime:
    expires: datetime = conn.execute(
        text(
            """
            INSERT INTO email_approvals (id, tenant_id, action_id, version, draft_sha256,
                actor_user_id, session_id, token_sha256, expires_at)
            VALUES (:id, :t, :a, :v, :d, :u, :s, :tok,
                    clock_timestamp() + make_interval(secs => :ttl))
            RETURNING expires_at
            """
        ),
        {
            "id": approval_id,
            "t": tenant_id,
            "a": action_id,
            "v": version,
            "d": draft_sha,
            "u": actor,
            "s": session_id,
            "tok": token_sha,
            "ttl": ttl_seconds,
        },
    ).scalar_one()
    return expires


def load_approval_by_token(conn: Connection, token_sha: str) -> ApprovalRow | None:
    row = conn.execute(
        text(
            f"SELECT {_APPROVAL} FROM email_approvals WHERE token_sha256 = :t"  # noqa: S608
            " FOR UPDATE"
        ),
        {"t": token_sha},
    ).one_or_none()
    return _approval(row) if row else None


def load_approval(conn: Connection, approval_id: UUID) -> ApprovalRow | None:
    row = conn.execute(
        text(f"SELECT {_APPROVAL} FROM email_approvals WHERE id = :id"),  # noqa: S608
        {"id": approval_id},
    ).one_or_none()
    return _approval(row) if row else None


def active_approval(conn: Connection, action_id: UUID) -> ApprovalRow | None:
    row = conn.execute(
        text(
            f"SELECT {_APPROVAL} FROM email_approvals"  # noqa: S608
            " WHERE action_id = :a AND status = 'active'"
        ),
        {"a": action_id},
    ).one_or_none()
    return _approval(row) if row else None


def consume_approval(conn: Connection, approval_id: UUID) -> bool:
    return (
        conn.execute(
            text(
                "UPDATE email_approvals SET status = 'consumed', consumed_at = clock_timestamp()"
                " WHERE id = :id AND status = 'active' AND expires_at > clock_timestamp()"
            ),
            {"id": approval_id},
        ).rowcount
        == 1
    )


_OUTBOX = """execution_key, tenant_id, action_id, version, approval_id, message_id, status,
    status_reason, attempts, reconcile_checks, claimed_at, smtp_accepted_at,
    uncertain_since, confirmed_at, mailpit_id"""


def _outbox(row: Any) -> OutboxRow:
    return OutboxRow(
        row.execution_key,
        row.tenant_id,
        row.action_id,
        row.version,
        row.approval_id,
        row.message_id,
        row.status,
        row.status_reason,
        row.attempts,
        row.reconcile_checks,
        row.claimed_at,
        row.smtp_accepted_at,
        row.uncertain_since,
        row.confirmed_at,
        row.mailpit_id,
    )


def insert_outbox(
    conn: Connection,
    *,
    execution_key: UUID,
    tenant_id: UUID,
    action_id: UUID,
    version: int,
    approval_id: UUID,
    message_id: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO email_outbox (execution_key, tenant_id, action_id, version, approval_id,
                                      message_id)
            VALUES (:k, :t, :a, :v, :ap, :m)
            """
        ),
        {
            "k": execution_key,
            "t": tenant_id,
            "a": action_id,
            "v": version,
            "ap": approval_id,
            "m": message_id,
        },
    )


def load_outbox(conn: Connection, execution_key: UUID, *, lock: bool = False) -> OutboxRow | None:
    sql = f"SELECT {_OUTBOX} FROM email_outbox WHERE execution_key = :k"  # noqa: S608
    if lock:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), {"k": execution_key}).one_or_none()
    return _outbox(row) if row else None


def outbox_for_action(conn: Connection, action_id: UUID, *, lock: bool = False) -> OutboxRow | None:
    sql = f"SELECT {_OUTBOX} FROM email_outbox WHERE action_id = :a"  # noqa: S608
    if lock:
        sql += " FOR UPDATE"
    row = conn.execute(text(sql), {"a": action_id}).one_or_none()
    return _outbox(row) if row else None


def update_outbox(conn: Connection, execution_key: UUID, **values: Any) -> None:
    """Update named columns; ``"now"`` means the database clock."""

    allowed = {
        "status",
        "status_reason",
        "attempts",
        "reconcile_checks",
        "dispatched_at",
        "claimed_at",
        "smtp_accepted_at",
        "uncertain_since",
        "confirmed_at",
        "mailpit_id",
    }
    sets, params = ["updated_at = clock_timestamp()"], {"k": execution_key}
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(key)
        if value == "now":
            sets.append(f"{key} = clock_timestamp()")
        else:
            sets.append(f"{key} = :{key}")
            params[key] = value
    conn.execute(
        text(f"UPDATE email_outbox SET {', '.join(sets)} WHERE execution_key = :k"),  # noqa: S608
        params,
    )


def mark_outbox_dispatched(conn: Connection, execution_key: UUID) -> None:
    update_outbox(conn, execution_key, dispatched_at="now")


# -- briefings ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    recipient_user_id: UUID
    question: str
    interval_seconds: int
    next_run_at: datetime
    active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OccurrenceRow:
    id: UUID
    schedule_id: UUID
    due_at: datetime
    policy_epoch: int | None
    run_id: UUID | None
    action_id: UUID | None
    status: str
    status_reason: str | None


_SCHEDULE = """id, tenant_id, owner_user_id, recipient_user_id, question, interval_seconds,
    next_run_at, active, created_at"""


def _schedule(row: Any) -> ScheduleRow:
    return ScheduleRow(
        row.id,
        row.tenant_id,
        row.owner_user_id,
        row.recipient_user_id,
        row.question,
        row.interval_seconds,
        row.next_run_at,
        row.active,
        row.created_at,
    )


def insert_schedule(conn: Connection, s: ScheduleRow) -> None:
    conn.execute(
        text(
            """
            INSERT INTO briefing_schedules (id, tenant_id, owner_user_id, recipient_user_id,
                question, interval_seconds, next_run_at)
            VALUES (:id, :t, :o, :r, :q, :i, :n)
            """
        ),
        {
            "id": s.id,
            "t": s.tenant_id,
            "o": s.owner_user_id,
            "r": s.recipient_user_id,
            "q": s.question,
            "i": s.interval_seconds,
            "n": s.next_run_at,
        },
    )


def load_schedule(
    conn: Connection, schedule_id: UUID, owner: UUID | None = None, *, lock: bool = False
) -> ScheduleRow | None:
    sql = f"SELECT {_SCHEDULE} FROM briefing_schedules WHERE id = :id"  # noqa: S608
    params: dict[str, Any] = {"id": schedule_id}
    if owner is not None:
        sql += " AND owner_user_id = :o"
        params["o"] = owner
    if lock:
        sql += " FOR UPDATE SKIP LOCKED"
    row = conn.execute(text(sql), params).one_or_none()
    return _schedule(row) if row else None


def list_schedules(conn: Connection, owner: UUID) -> list[ScheduleRow]:
    rows = conn.execute(
        text(
            f"SELECT {_SCHEDULE} FROM briefing_schedules WHERE owner_user_id = :o"  # noqa: S608
            " ORDER BY created_at DESC"
        ),
        {"o": owner},
    ).all()
    return [_schedule(r) for r in rows]


_OCCURRENCE = "id, schedule_id, due_at, policy_epoch, run_id, action_id, status, status_reason"


def _occurrence(row: Any) -> OccurrenceRow:
    return OccurrenceRow(
        row.id,
        row.schedule_id,
        row.due_at,
        row.policy_epoch,
        row.run_id,
        row.action_id,
        row.status,
        row.status_reason,
    )


def list_occurrences(conn: Connection, schedule_id: UUID, limit: int = 20) -> list[OccurrenceRow]:
    rows = conn.execute(
        text(
            f"SELECT {_OCCURRENCE} FROM briefing_occurrences"  # noqa: S608
            " WHERE schedule_id = :s ORDER BY due_at DESC LIMIT :n"
        ),
        {"s": schedule_id, "n": limit},
    ).all()
    return [_occurrence(r) for r in rows]


def load_occurrence(
    conn: Connection, occurrence_id: UUID, *, lock: bool = False
) -> OccurrenceRow | None:
    sql = f"SELECT {_OCCURRENCE} FROM briefing_occurrences WHERE id = :id"  # noqa: S608
    if lock:
        sql += " FOR UPDATE SKIP LOCKED"
    row = conn.execute(text(sql), {"id": occurrence_id}).one_or_none()
    return _occurrence(row) if row else None


def update_occurrence(conn: Connection, occurrence_id: UUID, **values: Any) -> None:
    allowed = {"status", "status_reason", "run_id", "action_id"}
    sets, params = ["updated_at = clock_timestamp()"], {"id": occurrence_id}
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(key)
        sets.append(f"{key} = :{key}")
        params[key] = value
    conn.execute(
        text(f"UPDATE briefing_occurrences SET {', '.join(sets)} WHERE id = :id"),  # noqa: S608
        params,
    )


# -- SMTP-start records -----------------------------------------------------------------


def record_smtp_attempt(
    conn: Connection, *, tenant_id: UUID, execution_key: UUID, attempt: int
) -> None:
    conn.execute(
        text(
            "INSERT INTO email_smtp_attempts (tenant_id, execution_key, attempt)"
            " VALUES (:t, :k, :a) ON CONFLICT DO NOTHING"
        ),
        {"t": tenant_id, "k": execution_key, "a": attempt},
    )


def smtp_attempted(conn: Connection, execution_key: UUID, attempt: int) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM email_smtp_attempts WHERE execution_key = :k AND attempt = :a"),
            {"k": execution_key, "a": attempt},
        ).first()
        is not None
    )


def lock_action_then_outbox(
    conn: Connection, execution_key: UUID, *, skip_locked: bool = False
) -> tuple[ActionRow, OutboxRow] | None | str:
    """Canonical lock order for M6 rows: (tenant policy epoch, if taken) -> email_actions
    -> email_outbox -> email_approvals. Cancel and send lock the action first, so every
    worker path does the same. Returns ``"busy"`` when ``skip_locked`` and another
    transaction holds the action (for example a live SMTP handoff)."""

    action_id = conn.execute(
        text("SELECT action_id FROM email_outbox WHERE execution_key = :k"), {"k": execution_key}
    ).scalar_one_or_none()
    if action_id is None:
        return None
    sql = f"SELECT {_ACTION} FROM email_actions WHERE id = :id FOR UPDATE"  # noqa: S608
    if skip_locked:
        sql += " SKIP LOCKED"
    row = conn.execute(text(sql), {"id": action_id}).one_or_none()
    if row is None:
        return "busy"
    outbox = load_outbox(conn, execution_key, lock=True)
    assert outbox is not None  # noqa: S101 - rows are never deleted
    return _action(row), outbox

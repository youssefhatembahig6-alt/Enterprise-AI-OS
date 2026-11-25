"""Current-state checks shared by drafting, approval, send and worker dispatch.

Every check reads PostgreSQL now (tenant RLS in force); nothing is taken from the
request, the approval or an earlier snapshot. Returns a reason code or ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection

from app.actions import store
from app.actions.render import source_sha256
from app.reporting import store as report_store
from app.reporting.contracts import RunStatus
from app.reporting.service import content_decision
from app.repositories.identity import load_subject
from app.tenancy.config import load_settings

ACTIONS_PACK = "actions"
MAIL_CONNECTOR = "mailpit"
SUPPORTED_SOURCE_KINDS = frozenset({"report"})


@dataclass(frozen=True, slots=True)
class GateResult:
    reason: str | None
    run: report_store.RunRow | None = None
    recipient: store.UserRow | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def capability(conn: Connection, *, delivery: bool) -> str | None:
    settings = load_settings(conn)
    if ACTIONS_PACK not in settings.enabled_packs:
        return "pack_disabled"
    if delivery and MAIL_CONNECTOR not in settings.enabled_connectors:
        return "connector_disabled"
    return None


def check(
    conn: Connection,
    *,
    owner_user_id: UUID,
    source_run_id: UUID,
    recipient_user_id: UUID,
    delivery: bool,
    expected_source_sha: str | None = None,
    expected_email: str | None = None,
) -> GateResult:
    """Sender, tenant configuration, source and recipient, in that order."""

    sender = load_subject(conn, owner_user_id)
    if sender is None or not sender.active:
        return GateResult("sender_inactive")
    if reason := capability(conn, delivery=delivery):
        return GateResult(reason)
    run = report_store.load_run(conn, source_run_id, owner_user_id)
    if run is None or run.status is not RunStatus.PUBLISHED or run.result is None:
        return GateResult("source_unavailable")
    if run.kind not in SUPPORTED_SOURCE_KINDS:
        return GateResult("unsupported_source", run)
    if not content_decision(conn, owner_user_id, run).allowed:
        return GateResult("source_access_revoked", run)
    if expected_source_sha is not None and source_sha256(run.result) != expected_source_sha:
        return GateResult("source_changed", run)
    recipient = store.load_user(conn, recipient_user_id)
    if recipient is None:
        return GateResult("recipient_not_found", run)
    if not recipient.active:
        return GateResult("recipient_inactive", run, recipient)
    if expected_email is not None and recipient.email.lower() != expected_email.lower():
        return GateResult("recipient_changed", run, recipient)
    if not content_decision(conn, recipient_user_id, run).allowed:
        return GateResult("recipient_not_authorized", run, recipient)
    return GateResult(None, run, recipient)

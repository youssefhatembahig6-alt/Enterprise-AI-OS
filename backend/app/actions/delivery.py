"""Outbox delivery, crash recovery and reconciliation (worker side).

State machine of ``email_outbox.status`` (ADR 0601)::

    pending --claim--> dispatching --250--> delivered
       ^                   |  |
       |                   |  +-- not sent (before DATA) --> pending (retry, same key)
       |                   |  +-- 5xx rejection ----------> failed
       |                   |  +-- ambiguous after DATA ---> uncertain
       |                   +-- lease expired (crash) -----> uncertain
       |                                                        |
       +--- never reached SMTP (no SMTP-start record) <---------+--> delivered (found)
                                                                |
                         SMTP may have been reached and the receipt is missing:
                         stays uncertain (never re-sent automatically)
    pending/dispatching --re-check fails at claim or handoff--> blocked
    pending --owner cancels--> cancelled

The claim commits ``dispatching`` *before* any byte goes to SMTP, so a crash at any
later point leaves a row that recovery can see. The handoff transaction re-checks
access under the tenant policy-epoch lock and keeps that lock through SMTP, so access
lost after the claim blocks the send. An ``uncertain`` row is re-sent only if a durable
SMTP-start record proves its attempt never reached SMTP; otherwise it must be found in
Mailpit by its Message-ID (delivered) or it stays uncertain. A retry re-runs every access check and
reuses the same execution key and Message-ID.

Lock order everywhere: tenant policy epoch (share) -> email_actions -> email_outbox ->
email_approvals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.actions import gate, store
from app.actions.mail import (
    Ambiguous,
    Mailer,
    MailLookup,
    MailpitUnavailable,
    NotSent,
    Rejected,
    build_message,
)
from app.actions.render import draft_sha256
from app.config import Settings
from app.db.engine import contextless_transaction, create_app_engine, tenant_transaction
from app.repositories.audit import record_audit_event

logger = logging.getLogger("eaios.actions")

Hook = Callable[[store.OutboxRow], None]


@dataclass
class EmailDelivery:
    engine: Engine
    settings: Settings
    mailer: Mailer
    lookup: MailLookup
    # Test-only fault injection points: "claim_locked" (claim holds the action and
    # outbox locks), "before_smtp" (after the claim committed, before the handoff
    # recheck), "smtp_started" (SMTP-start record committed, nothing sent yet) and
    # "after_accept" (Mailpit returned 250, confirmation not committed).
    hooks: dict[str, Hook] = field(default_factory=dict)
    # SMTP-start records are written while the handoff holds a pooled connection and
    # row locks. A separate small engine keeps that insert (which never waits on row
    # locks) from competing with transactions queued behind those locks for the main
    # pool, which could otherwise exhaust it and stall the handoff.
    marker_engine: Engine | None = None

    def __post_init__(self) -> None:
        if self.marker_engine is None:
            self.marker_engine = create_app_engine(
                self.engine.url.render_as_string(hide_password=False),
                pool_size=1,
                application_name="eaios-smtp-markers",
            )

    def close(self) -> None:
        if self.marker_engine is not None:
            self.marker_engine.dispose()

    # Dispatcher protocol (InlineDispatcher / Celery task call this).
    def run_job(self, execution_key: UUID, tenant_id: UUID) -> str:
        return self.deliver(execution_key, tenant_id)

    def _audit(
        self,
        conn: Any,
        row: store.OutboxRow,
        owner: UUID | None,
        decision: str,
        reason: str,
        **extra: Any,
    ) -> None:
        record_audit_event(
            conn,
            request_id=None,
            tenant_id=row.tenant_id,
            user_id=owner,
            event_type="action.email",
            action="email.deliver",
            decision=decision,
            reason=reason,
            details={
                "action_id": str(row.action_id),
                "execution_key": str(row.execution_key),
                "attempt": row.attempts,
                **extra,
            },
        )

    # -- delivery ------------------------------------------------------------------

    def deliver(self, execution_key: UUID, tenant_id: UUID) -> str:
        """Claim, then hand off under a recheck that holds the locks through SMTP.

        1. ``_claim`` commits ``dispatching`` before any SMTP traffic, so a crash at any
           later point leaves a row recovery can see.
        2. ``_handoff`` opens a new transaction, takes the tenant policy-epoch share lock
           and the action and outbox row locks, re-runs every check, and only then
           talks SMTP and records the outcome in that same transaction. An
           authorization change (it raises the policy epoch) therefore either commits
           before the recheck and blocks the send, or waits until the handoff is over:
           access lost after the claim can never be missed. A process that dies during
           the handoff releases the locks and rolls back to ``dispatching``.
        """

        claimed = self._claim(execution_key, tenant_id)
        if isinstance(claimed, str):
            return claimed
        if hook := self.hooks.get("before_smtp"):
            hook(claimed)
        return self._handoff(claimed)

    def _claim(self, execution_key: UUID, tenant_id: UUID) -> str | store.OutboxRow:
        with tenant_transaction(self.engine, tenant_id) as conn:
            conn.execute(text("SELECT lock_tenant_policy_epoch()"))
            locked = store.lock_action_then_outbox(conn, execution_key)
            if locked is None or isinstance(locked, str):
                return "missing"
            action, row = locked
            if hook := self.hooks.get("claim_locked"):
                hook(row)
            if row.status != "pending":
                return f"not_pending:{row.status}"
            version = store.load_version(conn, row.action_id, row.version)
            approval = store.load_approval(conn, row.approval_id)
            assert version is not None  # noqa: S101 - FK
            # A retry after an uncertain outcome must not send a second copy if the
            # first one did arrive.
            if row.attempts > 0:
                try:
                    found = self.lookup.find(row.message_id, version.recipient_email)
                except MailpitUnavailable:
                    return "lookup_unavailable"  # stays pending; retried by the beat
                if found is not None:
                    self._mark_delivered(
                        conn, row, action.owner_user_id, found, "found_before_retry"
                    )
                    return "delivered"
            reason = self._still_allowed(conn, row, action, version, approval)
            if reason is not None:
                self._block(conn, row, action, reason)
                return "blocked"
            if row.attempts >= self.settings.email_max_attempts:
                store.update_outbox(
                    conn, row.execution_key, status="failed", status_reason="attempts_exhausted"
                )
                store.set_action(conn, action.id, status="failed", reason="attempts_exhausted")
                self._audit(conn, row, action.owner_user_id, "info", "attempts_exhausted")
                return "failed"
            store.update_outbox(
                conn,
                row.execution_key,
                status="dispatching",
                attempts=row.attempts + 1,
                claimed_at="now",
                status_reason=None,
            )
            store.set_action(conn, action.id, status="sending", reason=None)
            claimed = store.load_outbox(conn, row.execution_key)
            assert claimed is not None  # noqa: S101
            self._audit(conn, claimed, action.owner_user_id, "allow", "claimed")
        return claimed

    def _block(self, conn: Any, row: store.OutboxRow, action: store.ActionRow, reason: str) -> None:
        store.update_outbox(conn, row.execution_key, status="blocked", status_reason=reason)
        store.set_action(conn, action.id, status="blocked", reason=reason)
        self._audit(conn, row, action.owner_user_id, "deny", reason)

    def _handoff(self, claimed: store.OutboxRow) -> str:
        with tenant_transaction(self.engine, claimed.tenant_id) as conn:
            conn.execute(text("SELECT lock_tenant_policy_epoch()"))
            locked = store.lock_action_then_outbox(conn, claimed.execution_key)
            if locked is None or isinstance(locked, str):
                return "missing"
            action, row = locked
            if row.status != "dispatching" or row.attempts != claimed.attempts:
                return "superseded"
            version = store.load_version(conn, row.action_id, row.version)
            approval = store.load_approval(conn, row.approval_id)
            assert version is not None  # noqa: S101
            reason = self._still_allowed(conn, row, action, version, approval)
            if reason is not None:
                self._block(conn, row, action, reason)
                return "blocked"
            # Durable proof that this attempt may reach SMTP, committed on a separate
            # connection before the first byte is sent (the handoff transaction itself
            # rolls back if the worker dies). Without it, recovery knows the attempt
            # never reached SMTP and may retry; with it, a missing receipt is never
            # taken as proof of non-delivery.
            self._record_smtp_start(row)
            message = build_message(
                sender=self.settings.mail_from,
                recipient=version.recipient_email,
                subject=version.subject,
                body=version.body,
                message_id=row.message_id,
            )
            try:
                accepted = self.mailer.send(
                    message, _address(self.settings.mail_from), version.recipient_email
                )
            except NotSent as exc:
                return self._record_failure(conn, row, action, "not_sent", str(exc))
            except Rejected as exc:
                return self._record_failure(conn, row, action, "rejected", str(exc))
            except Ambiguous as exc:
                return self._record_failure(conn, row, action, "ambiguous", str(exc))
            if hook := self.hooks.get("after_accept"):
                hook(row)
            store.update_outbox(
                conn,
                row.execution_key,
                status="delivered",
                status_reason="smtp_accepted",
                smtp_accepted_at="now",
                confirmed_at="now",
            )
            store.set_action(conn, action.id, status="delivered", reason="smtp_accepted")
            self._audit(
                conn,
                row,
                action.owner_user_id,
                "allow",
                "smtp_accepted",
                smtp_reply=accepted.reply[:120],
            )
        return "delivered"

    def _still_allowed(
        self,
        conn: Any,
        row: store.OutboxRow,
        action: store.ActionRow,
        version: store.VersionRow,
        approval: store.ApprovalRow | None,
    ) -> str | None:
        """Everything the send depended on, re-checked at claim and again at handoff."""

        if action.status == "cancelled":
            return "cancelled"
        if action.version != row.version:
            return "draft_changed"
        if approval is None or approval.status != "consumed" or approval.version != row.version:
            return "approval_missing"
        if approval.actor_user_id != action.owner_user_id:
            return "approval_actor"
        recomputed = draft_sha256(
            tenant_id=row.tenant_id,
            action_id=action.id,
            version=version.version,
            recipient_user_id=version.recipient_user_id,
            recipient_email=version.recipient_email,
            subject=version.subject,
            body=version.body,
            source_sha=version.source_sha256,
        )
        if recomputed != version.draft_sha256 or approval.draft_sha256 != version.draft_sha256:
            return "draft_integrity"
        result = gate.check(
            conn,
            owner_user_id=action.owner_user_id,
            source_run_id=action.source_run_id,
            recipient_user_id=version.recipient_user_id,
            delivery=True,
            expected_source_sha=version.source_sha256,
            expected_email=version.recipient_email,
        )
        return result.reason

    def _record_failure(
        self, conn: Any, row: store.OutboxRow, action: store.ActionRow, kind: str, detail: str
    ) -> str:
        if kind == "not_sent":
            store.update_outbox(
                conn,
                row.execution_key,
                status="pending",
                status_reason="not_sent_retry",
                dispatched_at=None,
            )
            store.set_action(conn, action.id, status="queued", reason="retrying")
        elif kind == "rejected":
            store.update_outbox(
                conn, row.execution_key, status="failed", status_reason="smtp_rejected"
            )
            store.set_action(conn, action.id, status="failed", reason="smtp_rejected")
        else:
            store.update_outbox(
                conn,
                row.execution_key,
                status="uncertain",
                status_reason="smtp_ambiguous",
                uncertain_since="now",
            )
            store.set_action(conn, action.id, status="uncertain", reason="smtp_ambiguous")
        self._audit(
            conn, row, action.owner_user_id, "info", f"smtp_{kind}", smtp_detail=detail[:200]
        )
        return kind

    def _mark_delivered(
        self, conn: Any, row: store.OutboxRow, owner: UUID, mailpit_id: str, reason: str
    ) -> None:
        store.update_outbox(
            conn,
            row.execution_key,
            status="delivered",
            status_reason=reason,
            confirmed_at="now",
            mailpit_id=mailpit_id,
        )
        store.set_action(conn, row.action_id, status="delivered", reason=reason)
        self._audit(conn, row, owner, "allow", reason, mailpit_id=mailpit_id)

    def _record_smtp_start(self, row: store.OutboxRow) -> None:
        assert self.marker_engine is not None  # noqa: S101 - set in __post_init__
        with tenant_transaction(self.marker_engine, row.tenant_id) as conn:
            store.record_smtp_attempt(
                conn, tenant_id=row.tenant_id, execution_key=row.execution_key, attempt=row.attempts
            )
        if hook := self.hooks.get("smtp_started"):
            hook(row)

    # -- recovery ------------------------------------------------------------------

    def expire_lease(self, execution_key: UUID, tenant_id: UUID) -> str:
        """A claim older than the lease may have crashed at any point, including after
        Mailpit accepted the message: mark it uncertain (never pending). A live handoff
        holds the action lock, so it is skipped rather than waited for."""

        with tenant_transaction(self.engine, tenant_id) as conn:
            locked = store.lock_action_then_outbox(conn, execution_key, skip_locked=True)
            if locked == "busy":
                return "handoff_in_progress"
            if locked is None or isinstance(locked, str):
                return "missing"
            action, row = locked
            if row.status != "dispatching":
                return "not_dispatching"
            expired = conn.execute(
                text(
                    "SELECT clock_timestamp() - claimed_at > make_interval(secs => :s)"
                    " FROM email_outbox WHERE execution_key = :k"
                ),
                {"s": self.settings.email_dispatch_lease_seconds, "k": execution_key},
            ).scalar_one()
            if not expired:
                return "lease_active"
            store.update_outbox(
                conn,
                execution_key,
                status="uncertain",
                status_reason="dispatch_lease_expired",
                uncertain_since="now",
            )
            store.set_action(conn, action.id, status="uncertain", reason="dispatch_lease_expired")
            self._audit(conn, row, action.owner_user_id, "info", "dispatch_lease_expired")
        return "uncertain"

    def reconcile(self, execution_key: UUID, tenant_id: UUID) -> str:
        """Resolve an uncertain send.

        * No SMTP-start record for the current attempt: the attempt provably never
          reached SMTP (it died between the claim and the handoff), so it is re-queued
          with the same key and Message-ID. The claim and handoff re-check everything.
        * SMTP may have been reached: look the Message-ID and recipient up in Mailpit.
          Found -> delivered, no resend. Absent -> it stays ``uncertain``
          (``receipt_missing_after_smtp``): a missing receipt does not prove the server
          never accepted it (it may have been deleted or pruned), so it is never re-sent
          automatically. Mailpit unreachable -> stays uncertain.
        """

        with tenant_transaction(self.engine, tenant_id) as conn:
            locked = store.lock_action_then_outbox(conn, execution_key, skip_locked=True)
            if locked == "busy":
                return "handoff_in_progress"
            if locked is None or isinstance(locked, str):
                return "missing"
            action, row = locked
            if row.status != "uncertain":
                return "not_uncertain"
            if not store.smtp_attempted(conn, execution_key, row.attempts):
                store.update_outbox(
                    conn,
                    execution_key,
                    status="pending",
                    status_reason="never_reached_smtp_retry",
                    dispatched_at=None,
                )
                store.set_action(
                    conn, action.id, status="queued", reason="never_reached_smtp_retry"
                )
                self._audit(conn, row, action.owner_user_id, "info", "never_reached_smtp_retry")
                return "requeued"
            version = store.load_version(conn, row.action_id, row.version)
            assert version is not None  # noqa: S101
            try:
                found = self.lookup.find(row.message_id, version.recipient_email)
            except MailpitUnavailable as exc:
                self._audit(
                    conn,
                    row,
                    action.owner_user_id,
                    "info",
                    "reconcile_unavailable",
                    detail=str(exc)[:120],
                )
                return "unavailable"
            if found is not None:
                self._mark_delivered(conn, row, action.owner_user_id, found, "reconciled_found")
                return "delivered"
            reason = "receipt_missing_after_smtp"
            if row.status_reason != reason:
                store.update_outbox(conn, execution_key, status_reason=reason)
                store.set_action(conn, action.id, status="uncertain", reason=reason)
                self._audit(conn, row, action.owner_user_id, "info", reason)
        return "receipt_missing"


def _address(mail_from: str) -> str:
    from email.utils import parseaddr  # noqa: PLC0415

    return parseaddr(mail_from)[1]


@dataclass
class DueWork:
    tenant_id: UUID
    id: UUID
    kind: str


def due_work(engine: Engine, settings: Settings, redispatch_seconds: int = 30) -> list[DueWork]:
    with contextless_transaction(engine) as conn:
        rows = conn.execute(
            text("SELECT tenant_id, id, kind FROM action_work_due(:lease, :redispatch)"),
            {"lease": settings.email_dispatch_lease_seconds, "redispatch": redispatch_seconds},
        ).all()
    return [DueWork(r.tenant_id, r.id, r.kind) for r in rows]

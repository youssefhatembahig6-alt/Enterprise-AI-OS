"""Outbox delivery, crash recovery and reconciliation (worker side).

State machine of ``email_outbox.status`` (ADR 0601)::

    pending --claim--> dispatching --250--> delivered
       ^                   |  |
       |                   |  +-- not sent (before DATA) --> pending (retry, same key)
       |                   |  +-- 5xx rejection ----------> failed
       |                   |  +-- ambiguous after DATA ---> uncertain
       |                   +-- lease expired (crash) -----> uncertain
       |                                                        |
       +--- absent from Mailpit N times (reconcile) <-----------+--> delivered (found)
            (only while the receipt-history canary
             proves Mailpit's history covers the send;
             otherwise it stays uncertain)
    pending/dispatching --re-check fails at claim or handoff--> blocked
    pending --owner cancels--> cancelled

The claim commits ``dispatching`` *before* any byte goes to SMTP, so a crash at any
later point leaves a row that recovery can see. The handoff transaction re-checks
access under the tenant policy-epoch lock and keeps that lock through SMTP, so access
lost after the claim blocks the send. An ``uncertain`` row is never blindly re-sent: it
is first looked up in Mailpit by its Message-ID. A retry re-runs every access check and
reuses the same execution key and Message-ID.

Lock order everywhere: tenant policy epoch (share) -> email_actions -> email_outbox ->
email_approvals.
"""

from __future__ import annotations

import logging
import uuid
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
from app.db.engine import contextless_transaction, tenant_transaction
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
    # recheck) and "after_accept" (Mailpit returned 250, confirmation not committed).
    hooks: dict[str, Hook] = field(default_factory=dict)

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

        if not self.ensure_canary(tenant_id):
            return "canary_unavailable"  # stays pending; the beat retries
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

    # -- receipt history -------------------------------------------------------------

    def ensure_canary(self, tenant_id: UUID) -> bool:
        """Make sure the tenant has an active receipt-history canary in Mailpit (sent and
        recorded before any send it will vouch for)."""

        with tenant_transaction(self.engine, tenant_id) as conn:
            if store.active_canary(conn) is not None:
                return True
        canary_id = uuid.uuid4()
        domain = self.settings.mail_message_id_domain
        message_id = f"<canary-{canary_id}@{domain}>"
        recipient = f"receipt-canary@{domain}"
        message = build_message(
            sender=self.settings.mail_from,
            recipient=recipient,
            subject="Enterprise AI OS receipt-history canary (do not delete)",
            body="Kept so the application can tell whether this mail store still holds"
            " the history of earlier sends. Deleting it makes uncertain sends stay"
            " uncertain instead of being retried.",
            message_id=message_id,
        )
        message["X-EAIOS-Canary"] = "1"
        try:
            self.mailer.send(message, _address(self.settings.mail_from), recipient)
        except (NotSent, Rejected, Ambiguous):
            return False
        with tenant_transaction(self.engine, tenant_id) as conn:
            created = store.insert_canary(
                conn,
                canary_id=canary_id,
                tenant_id=tenant_id,
                message_id=message_id,
                recipient=recipient,
            )
            record_audit_event(
                conn,
                request_id=None,
                tenant_id=tenant_id,
                user_id=None,
                event_type="action.email",
                action="email.receipt_canary",
                decision="info",
                reason="canary_created" if created else "canary_exists",
                details={"canary_id": str(canary_id)},
            )
        return True

    def _history_covers(self, conn: Any, row: store.OutboxRow) -> bool:
        """True only if Mailpit still holds a canary accepted before this send's claim,
        i.e. its receipt history covers the send (it was not reset, pruned or replaced
        by a non-persistent store since)."""

        if row.claimed_at is None:
            return False
        canary = store.covering_canary(conn, row.claimed_at)
        if canary is None:
            return False
        found = self.lookup.find(canary.message_id, canary.recipient)  # may raise
        if found is None:
            store.mark_canary(conn, canary.id, "missing")
            return False
        return True

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
        """Resolve an uncertain send against Mailpit by Message-ID and recipient. Absence
        counts only while Mailpit's receipt history provably covers the send."""

        with tenant_transaction(self.engine, tenant_id) as conn:
            locked = store.lock_action_then_outbox(conn, execution_key, skip_locked=True)
            if locked == "busy":
                return "handoff_in_progress"
            if locked is None or isinstance(locked, str):
                return "missing"
            action, row = locked
            if row.status != "uncertain":
                return "not_uncertain"
            version = store.load_version(conn, row.action_id, row.version)
            assert version is not None  # noqa: S101
            try:
                found = self.lookup.find(row.message_id, version.recipient_email)
                covered = found is not None or self._history_covers(conn, row)
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
            if not covered:
                store.update_outbox(
                    conn, execution_key, status_reason="receipt_history_unavailable"
                )
                store.set_action(
                    conn, action.id, status="uncertain", reason="receipt_history_unavailable"
                )
                self._audit(conn, row, action.owner_user_id, "info", "receipt_history_unavailable")
                return "history_unavailable"
            checks = row.reconcile_checks + 1
            if checks < self.settings.email_reconcile_absent_checks:
                store.update_outbox(conn, execution_key, reconcile_checks=checks)
                self._audit(
                    conn, row, action.owner_user_id, "info", "reconcile_absent", checks=checks
                )
                return "absent"
            # Confirmed absent from a store whose history covers the send: safe to try
            # again with the same key and Message-ID. The claim and the handoff re-run
            # every access check and look Mailpit up once more.
            store.update_outbox(
                conn,
                execution_key,
                status="pending",
                reconcile_checks=0,
                status_reason="reconciled_absent_retry",
                dispatched_at=None,
            )
            store.set_action(conn, action.id, status="queued", reason="reconciled_absent_retry")
            self._audit(
                conn, row, action.owner_user_id, "info", "reconciled_absent_retry", checks=checks
            )
        return "requeued"


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

"""Email drafts, edits, approvals, sends and briefing schedules (request side).

Invariants (ADR 0601):

* A draft is built only from a published verified report owned by the caller. Its
  versions are immutable; an edit inserts a new version and invalidates approvals.
* An approval binds tenant (RLS), actor, session, action, version and draft hash, has a
  short expiry, is stored only as a token hash and can be consumed once.
* Sending re-checks sender, tenant configuration, source access and recipient access
  from current PostgreSQL state inside the transaction that consumes the approval and
  writes the outbox row, under the tenant policy-epoch share lock.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.actions import gate, store
from app.actions.contracts import (
    ApprovalOut,
    DeliveryOut,
    DraftCreate,
    DraftEdit,
    DraftOut,
    OccurrenceOut,
    RecipientOut,
    ScheduleCreate,
    ScheduleOut,
    SendRequest,
    VersionRef,
)
from app.actions.render import UnsupportedSource, draft_sha256, render_report, source_sha256
from app.config import Settings
from app.db.engine import tenant_transaction
from app.reporting import store as report_store
from app.reporting.service import content_decision
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext

EDITABLE = frozenset({"draft", "approved"})
TERMINAL = frozenset({"delivered", "blocked", "failed", "cancelled"})


class ActionError(Exception):
    def __init__(self, status: int, code: str, message: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.reason = status, code, message, reason


class Denied(Exception):
    """A refused operation. The transaction that found it rolls back; the denial is then
    audited in its own transaction so the record survives."""

    def __init__(self, op: str, subject_id: UUID | None, reason: str, error: ActionError) -> None:
        super().__init__(reason)
        self.op, self.subject_id, self.reason, self.error = op, subject_id, reason, error


def not_found() -> ActionError:
    return ActionError(404, "not_found", "Not found.")


_GATE_MESSAGES = {
    "sender_inactive": "Your account can no longer send email.",
    "pack_disabled": "Email actions are not enabled for this organization.",
    "connector_disabled": "Email delivery is not enabled for this organization.",
    "source_unavailable": "The source report is not available.",
    "unsupported_source": "Only published verified reports can be emailed in this version.",
    "source_access_revoked": "You no longer have access to the source report.",
    "source_changed": "The source report changed; create a new draft.",
    "recipient_not_found": "The recipient is not a user in your organization.",
    "recipient_inactive": "The recipient's account is not active.",
    "recipient_changed": "The recipient's address changed; review and approve again.",
    "recipient_not_authorized": "The recipient is not authorized to see this report.",
}

_APPROVAL_MESSAGES = {
    "approval_unknown": "The approval is not valid for this draft.",
    "approval_mismatch": "The approval is not valid for this draft.",
    "approval_actor": "The approval is not valid for this draft.",
    "approval_expired": "The approval has expired. Approve the draft again.",
    "approval_used": "The approval was already used.",
    "approval_superseded": "The draft changed after approval. Review and approve it again.",
}


def gate_error(reason: str) -> ActionError:
    if reason in ("source_unavailable",):
        return not_found()
    return ActionError(422, "action_blocked", _GATE_MESSAGES.get(reason, "Blocked."), reason)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class ActionService:
    engine: Engine
    settings: Settings
    dispatch: Callable[[UUID, UUID], None] | None
    create_run: Callable[..., report_store.RunRow] | None = None

    # -- helpers -------------------------------------------------------------------

    def _audit(
        self,
        conn: Any,
        ctx: AccessContext | None,
        *,
        tenant_id: UUID,
        user_id: UUID | None,
        action: str,
        decision: str,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        record_audit_event(
            conn,
            request_id=ctx.request_id if ctx else None,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="action.email" if action.startswith("email.") else "action.briefing",
            action=action,
            decision=decision,
            reason=reason,
            details=details,
        )

    def _draft_out(self, conn: Any, action: store.ActionRow, viewer: UUID) -> DraftOut:
        version = store.load_version(conn, action.id, action.version)
        assert version is not None  # noqa: S101 - FK guarantees the current version
        recipient = store.load_user(conn, version.recipient_user_id)
        run = report_store.load_run(conn, action.source_run_id, action.owner_user_id)
        # The body carries report figures: show it only while the viewer may still see
        # the source content (same rule as run replay and evidence).
        withheld = run is None or not content_decision(conn, viewer, run).allowed
        approval = store.active_approval(conn, action.id)
        outbox = store.outbox_for_action(conn, action.id)
        return DraftOut(
            action_id=action.id,
            status=action.status,
            status_reason=action.status_reason,
            origin=action.origin,
            source_run_id=action.source_run_id,
            version=action.version,
            draft_sha256=version.draft_sha256,
            recipient=RecipientOut(
                user_id=version.recipient_user_id,
                display_name=recipient.display_name if recipient else "",
                email=version.recipient_email,
            ),
            subject=None if withheld else version.subject,
            body=None if withheld else version.body,
            content_withheld=withheld,
            edited=version.edited_by is not None,
            approval_expires_at=approval.expires_at if approval and not approval.expired else None,
            delivery=_delivery(outbox),
            created_at=action.created_at,
            updated_at=action.updated_at,
        )

    # -- recipients ----------------------------------------------------------------

    def recipients(self, ctx: AccessContext) -> list[RecipientOut]:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            if reason := gate.capability(conn, delivery=False):
                raise gate_error(reason)
            return [
                RecipientOut(user_id=u.id, display_name=u.display_name, email=u.email)
                for u in store.list_active_users(conn)
            ]

    # -- drafts --------------------------------------------------------------------

    def create_draft(self, ctx: AccessContext, body: DraftCreate) -> DraftOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            action = create_draft_in(
                conn,
                tenant_id=ctx.tenant_id,
                owner_user_id=ctx.user_id,
                source_run_id=body.source_run_id,
                recipient_user_id=body.recipient_user_id,
                origin="user",
                sender_name=ctx.display_name,
                request_id=ctx.request_id,
            )
            return self._draft_out(conn, action, ctx.user_id)

    def list_drafts(self, ctx: AccessContext) -> list[DraftOut]:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            return [
                self._draft_out(conn, a, ctx.user_id) for a in store.list_actions(conn, ctx.user_id)
            ]

    def get_draft(self, ctx: AccessContext, action_id: UUID) -> DraftOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            action = store.load_action(conn, action_id, ctx.user_id)
            if action is None:
                raise not_found()
            return self._draft_out(conn, action, ctx.user_id)

    def _locked_current(
        self, conn: Any, ctx: AccessContext, action_id: UUID, ref: VersionRef
    ) -> tuple[store.ActionRow, store.VersionRow]:
        action = store.load_action(conn, action_id, ctx.user_id, lock=True)
        if action is None:
            raise not_found()
        if action.status not in EDITABLE:
            raise ActionError(409, "action_locked", f"The draft is {action.status}.")
        current = store.load_version(conn, action.id, action.version)
        assert current is not None  # noqa: S101
        if ref.version != action.version or ref.draft_sha256 != current.draft_sha256:
            raise ActionError(
                409, "version_conflict", "The draft changed. Reload it and review again."
            )
        recomputed = draft_sha256(
            tenant_id=ctx.tenant_id,
            action_id=action.id,
            version=current.version,
            recipient_user_id=current.recipient_user_id,
            recipient_email=current.recipient_email,
            subject=current.subject,
            body=current.body,
            source_sha=current.source_sha256,
        )
        if recomputed != current.draft_sha256:
            raise ActionError(
                409, "draft_integrity", "The stored draft failed its integrity check."
            )
        return action, current

    def edit(self, ctx: AccessContext, action_id: UUID, body: DraftEdit) -> DraftOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            action, current = self._locked_current(conn, ctx, action_id, body)
            result = gate.check(
                conn,
                owner_user_id=ctx.user_id,
                source_run_id=action.source_run_id,
                recipient_user_id=body.recipient_user_id,
                delivery=False,
                expected_source_sha=current.source_sha256,
            )
            if not result.ok or result.recipient is None:
                reason = result.reason or "blocked"
                raise Denied("email.edit", action.id, reason, gate_error(reason))
            new_version = action.version + 1
            sha = draft_sha256(
                tenant_id=ctx.tenant_id,
                action_id=action.id,
                version=new_version,
                recipient_user_id=result.recipient.id,
                recipient_email=result.recipient.email,
                subject=body.subject,
                body=body.body,
                source_sha=current.source_sha256,
            )
            store.insert_version(
                conn,
                ctx.tenant_id,
                store.VersionRow(
                    action.id,
                    new_version,
                    result.recipient.id,
                    result.recipient.email,
                    body.subject,
                    body.body,
                    current.source_sha256,
                    sha,
                    ctx.user_id,
                    datetime.now(UTC),
                ),
            )
            invalidated = store.invalidate_approvals(conn, action.id)
            store.set_action(conn, action.id, status="draft", reason=None, version=new_version)
            self._audit(
                conn,
                ctx,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                action="email.edit",
                decision="info",
                reason="new_version",
                details={
                    "action_id": str(action.id),
                    "version": new_version,
                    "approvals_invalidated": invalidated,
                },
            )
            refreshed = store.load_action(conn, action.id, ctx.user_id)
            assert refreshed is not None  # noqa: S101
            return self._draft_out(conn, refreshed, ctx.user_id)

    def record_denial(self, ctx: AccessContext, denied: Denied) -> None:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            self._audit(
                conn,
                ctx,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                action=denied.op,
                decision="deny",
                reason=denied.reason,
                details={"subject_id": str(denied.subject_id) if denied.subject_id else None},
            )

    # -- approval and send -----------------------------------------------------------

    def approve(self, ctx: AccessContext, action_id: UUID, ref: VersionRef) -> ApprovalOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            action, current = self._locked_current(conn, ctx, action_id, ref)
            result = gate.check(
                conn,
                owner_user_id=ctx.user_id,
                source_run_id=action.source_run_id,
                recipient_user_id=current.recipient_user_id,
                delivery=True,
                expected_source_sha=current.source_sha256,
                expected_email=current.recipient_email,
            )
            if not result.ok:
                reason = result.reason or "blocked"
                raise Denied("email.approve", action.id, reason, gate_error(reason))
            store.invalidate_approvals(conn, action.id)
            token = secrets.token_urlsafe(32)
            approval_id = uuid.uuid4()
            expires = store.insert_approval(
                conn,
                approval_id=approval_id,
                tenant_id=ctx.tenant_id,
                action_id=action.id,
                version=current.version,
                draft_sha=current.draft_sha256,
                actor=ctx.user_id,
                session_id=ctx.session_id,
                token_sha=token_hash(token),
                ttl_seconds=self.settings.action_approval_ttl_seconds,
            )
            store.set_action(conn, action.id, status="approved")
            self._audit(
                conn,
                ctx,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                action="email.approve",
                decision="allow",
                reason="approved",
                details={
                    "action_id": str(action.id),
                    "approval_id": str(approval_id),
                    "version": current.version,
                    "draft_sha256": current.draft_sha256,
                    "session_id": str(ctx.session_id),
                },
            )
        return ApprovalOut(
            approval_token=token,
            action_id=action.id,
            version=current.version,
            draft_sha256=current.draft_sha256,
            expires_at=expires,
        )

    def send(self, ctx: AccessContext, action_id: UUID, body: SendRequest) -> DraftOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            # Serialize with authorization changes (policy-epoch trigger) for the checks.
            conn.execute(text("SELECT lock_tenant_policy_epoch()"))
            action = store.load_action(conn, action_id, ctx.user_id, lock=True)
            if action is None:
                raise not_found()
            approval = store.load_approval_by_token(conn, token_hash(body.approval_token))
            reason = self._approval_problem(ctx, action, approval, body)
            if reason is not None:
                raise Denied(
                    "email.send",
                    action.id,
                    reason,
                    ActionError(409, "approval_rejected", _APPROVAL_MESSAGES[reason], reason),
                )
            assert approval is not None  # noqa: S101
            current = store.load_version(conn, action.id, action.version)
            assert current is not None  # noqa: S101
            if action.status != "approved":
                raise ActionError(409, "action_locked", f"The draft is {action.status}.")
            result = gate.check(
                conn,
                owner_user_id=ctx.user_id,
                source_run_id=action.source_run_id,
                recipient_user_id=current.recipient_user_id,
                delivery=True,
                expected_source_sha=current.source_sha256,
                expected_email=current.recipient_email,
            )
            if not result.ok:
                # The approval cannot be used for a send that is no longer allowed.
                store.invalidate_approvals(conn, action.id)
                store.set_action(conn, action.id, status="draft", reason=result.reason)
                self._audit(
                    conn,
                    ctx,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    action="email.send",
                    decision="deny",
                    reason=result.reason or "blocked",
                    details={"action_id": str(action.id), "approval_invalidated": True},
                )
                blocked = (action.id, result.reason or "blocked")
            else:
                blocked = None
                if not store.consume_approval(conn, approval.id):
                    raise ActionError(409, "approval_rejected", _APPROVAL_MESSAGES["approval_used"])
                key = uuid.uuid4()
                message_id = f"<{key}@{self.settings.mail_message_id_domain}>"
                store.insert_outbox(
                    conn,
                    execution_key=key,
                    tenant_id=ctx.tenant_id,
                    action_id=action.id,
                    version=current.version,
                    approval_id=approval.id,
                    message_id=message_id,
                )
                store.set_action(conn, action.id, status="queued")
                self._audit(
                    conn,
                    ctx,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    action="email.send",
                    decision="allow",
                    reason="queued",
                    details={
                        "action_id": str(action.id),
                        "approval_id": str(approval.id),
                        "execution_key": str(key),
                        "version": current.version,
                    },
                )
        if blocked is not None:
            raise gate_error(blocked[1])
        if self.dispatch is not None:
            self.dispatch(key, ctx.tenant_id)
        return self.get_draft(ctx, action_id)

    @staticmethod
    def _approval_problem(
        ctx: AccessContext,
        action: store.ActionRow,
        approval: store.ApprovalRow | None,
        body: SendRequest,
    ) -> str | None:
        if approval is None:
            return "approval_unknown"  # forged, or another tenant's (RLS hides it)
        if approval.action_id != action.id:
            return "approval_mismatch"
        if approval.actor_user_id != ctx.user_id or approval.session_id != ctx.session_id:
            return "approval_actor"
        if approval.status == "consumed":
            return "approval_used"
        if approval.status == "invalidated":
            return "approval_superseded"
        if approval.expired:
            return "approval_expired"
        if (
            approval.version != action.version
            or body.version != approval.version
            or body.draft_sha256 != approval.draft_sha256
        ):
            return "approval_superseded"
        return None

    def cancel(self, ctx: AccessContext, action_id: UUID) -> DraftOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            action = store.load_action(conn, action_id, ctx.user_id, lock=True)
            if action is None:
                raise not_found()
            if action.status in EDITABLE:
                store.invalidate_approvals(conn, action.id)
            elif action.status == "queued":
                outbox = store.outbox_for_action(conn, action.id, lock=True)
                if outbox is None or outbox.status != "pending":
                    raise ActionError(409, "too_late", "The email is already being sent.")
                store.update_outbox(
                    conn,
                    outbox.execution_key,
                    status="cancelled",
                    status_reason="cancelled_by_owner",
                )
            else:
                raise ActionError(409, "action_locked", f"The draft is {action.status}.")
            store.set_action(conn, action.id, status="cancelled", reason="cancelled_by_owner")
            self._audit(
                conn,
                ctx,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                action="email.cancel",
                decision="info",
                reason="cancelled_by_owner",
                details={"action_id": str(action.id), "previous_status": action.status},
            )
            refreshed = store.load_action(conn, action.id, ctx.user_id)
            assert refreshed is not None  # noqa: S101
            return self._draft_out(conn, refreshed, ctx.user_id)

    # -- briefing schedules ----------------------------------------------------------

    def create_schedule(
        self, ctx: AccessContext, body: ScheduleCreate, kind_of: Callable[[str], str]
    ) -> ScheduleOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            if reason := gate.capability(conn, delivery=False):
                raise gate_error(reason)
            if kind_of(body.question) != "report":
                raise ActionError(
                    422,
                    "unsupported_question",
                    "Scheduled briefings support verified sales report questions only.",
                )
            recipient = store.load_user(conn, body.recipient_user_id)
            if recipient is None or not recipient.active:
                raise gate_error(
                    "recipient_not_found" if recipient is None else "recipient_inactive"
                )
            row = store.ScheduleRow(
                uuid.uuid4(),
                ctx.tenant_id,
                ctx.user_id,
                recipient.id,
                body.question.strip(),
                body.interval_seconds,
                body.first_run_at,
                True,
                datetime.now(UTC),
            )
            store.insert_schedule(conn, row)
            self._audit(
                conn,
                ctx,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                action="briefing.create",
                decision="info",
                reason="schedule_created",
                details={"schedule_id": str(row.id), "interval_seconds": row.interval_seconds},
            )
            saved = store.load_schedule(conn, row.id, ctx.user_id)
            assert saved is not None  # noqa: S101
            return _schedule_out(conn, saved)

    def list_schedules(self, ctx: AccessContext) -> list[ScheduleOut]:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            return [_schedule_out(conn, s) for s in store.list_schedules(conn, ctx.user_id)]

    def deactivate_schedule(self, ctx: AccessContext, schedule_id: UUID) -> ScheduleOut:
        with tenant_transaction(self.engine, ctx.tenant_id, ctx.user_id) as conn:
            schedule = store.load_schedule(conn, schedule_id, ctx.user_id)
            if schedule is None:
                raise not_found()
            conn.execute(
                text(
                    "UPDATE briefing_schedules SET active = false, updated_at = clock_timestamp()"
                    " WHERE id = :id"
                ),
                {"id": schedule_id},
            )
            self._audit(
                conn,
                ctx,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                action="briefing.deactivate",
                decision="info",
                reason="schedule_deactivated",
                details={"schedule_id": str(schedule_id)},
            )
            saved = store.load_schedule(conn, schedule_id, ctx.user_id)
            assert saved is not None  # noqa: S101
            return _schedule_out(conn, saved)


def create_draft_in(
    conn: Any,
    *,
    tenant_id: UUID,
    owner_user_id: UUID,
    source_run_id: UUID,
    recipient_user_id: UUID,
    origin: str,
    sender_name: str,
    request_id: str | None,
) -> store.ActionRow:
    """Create version 1 of a draft inside the caller's tenant transaction. Used by the
    API and by scheduled briefings; never creates an approval."""

    result = gate.check(
        conn,
        owner_user_id=owner_user_id,
        source_run_id=source_run_id,
        recipient_user_id=recipient_user_id,
        delivery=False,
    )
    if not result.ok or result.run is None or result.recipient is None:
        reason = result.reason or "blocked"
        raise Denied("email.draft", source_run_id, reason, gate_error(reason))
    assert result.run.result is not None  # noqa: S101 - published runs carry a result
    try:
        subject, body = render_report(result.run.result, sender_name)
    except UnsupportedSource as exc:
        raise Denied(
            "email.draft", source_run_id, "unsupported_source", gate_error("unsupported_source")
        ) from exc
    action_id = uuid.uuid4()
    src = source_sha256(result.run.result)
    store.insert_action(
        conn,
        action_id=action_id,
        tenant_id=tenant_id,
        owner=owner_user_id,
        run_id=source_run_id,
        origin=origin,
    )
    sha = draft_sha256(
        tenant_id=tenant_id,
        action_id=action_id,
        version=1,
        recipient_user_id=result.recipient.id,
        recipient_email=result.recipient.email,
        subject=subject,
        body=body,
        source_sha=src,
    )
    store.insert_version(
        conn,
        tenant_id,
        store.VersionRow(
            action_id,
            1,
            result.recipient.id,
            result.recipient.email,
            subject,
            body,
            src,
            sha,
            None,
            datetime.now(UTC),
        ),
    )
    record_audit_event(
        conn,
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=owner_user_id,
        event_type="action.email",
        action="email.draft",
        decision="info",
        reason="draft_created",
        details={
            "action_id": str(action_id),
            "source_run_id": str(source_run_id),
            "origin": origin,
            "draft_sha256": sha,
        },
    )
    action = store.load_action(conn, action_id)
    assert action is not None  # noqa: S101
    return action


def _delivery(outbox: store.OutboxRow | None) -> DeliveryOut | None:
    if outbox is None:
        return None
    return DeliveryOut(
        execution_key=outbox.execution_key,
        message_id=outbox.message_id,
        status=outbox.status,
        status_reason=outbox.status_reason,
        attempts=outbox.attempts,
        smtp_accepted_at=outbox.smtp_accepted_at,
        confirmed_at=outbox.confirmed_at,
        uncertain_since=outbox.uncertain_since,
        mailpit_id=outbox.mailpit_id,
    )


def _schedule_out(conn: Any, s: store.ScheduleRow) -> ScheduleOut:
    recipient = store.load_user(conn, s.recipient_user_id)
    return ScheduleOut(
        schedule_id=s.id,
        question=s.question,
        recipient=RecipientOut(
            user_id=s.recipient_user_id,
            display_name=recipient.display_name if recipient else "",
            email=recipient.email if recipient else "",
        ),
        interval_seconds=s.interval_seconds,
        next_run_at=s.next_run_at,
        active=s.active,
        created_at=s.created_at,
        occurrences=[
            OccurrenceOut(
                occurrence_id=o.id,
                due_at=o.due_at,
                status=o.status,
                status_reason=o.status_reason,
                run_id=o.run_id,
                action_id=o.action_id,
            )
            for o in store.list_occurrences(conn, s.id)
        ],
    )

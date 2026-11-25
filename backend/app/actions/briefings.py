"""Scheduled briefings: run the owner's question with the owner's *current* identity and
permissions, then turn a published, verified result into a draft for review.

Nothing here can approve or send. Each occurrence reloads the owner from PostgreSQL
(active user and tenant, packs, policy epoch); the report run then authorizes the
dataset at every step as any run does; the draft re-checks source and recipient. A
saved access context from schedule creation is never reused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.actions import store
from app.actions.delivery import EmailDelivery, due_work
from app.actions.service import Denied, create_draft_in
from app.config import Settings
from app.db.engine import tenant_transaction
from app.reporting import store as report_store
from app.reporting.contracts import TERMINAL_STATUSES, RunStatus
from app.repositories.audit import record_audit_event
from app.repositories.identity import load_subject
from app.tenancy.config import load_settings

logger = logging.getLogger("eaios.actions.briefings")

CreateRun = Callable[..., report_store.RunRow]


@dataclass
class BriefingRunner:
    engine: Engine
    create_run: CreateRun

    def _audit(
        self,
        conn: Any,
        tenant_id: UUID,
        user_id: UUID | None,
        decision: str,
        reason: str,
        **details: Any,
    ) -> None:
        record_audit_event(
            conn,
            request_id=None,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="action.briefing",
            action="briefing.run",
            decision=decision,
            reason=reason,
            details={k: str(v) if isinstance(v, UUID) else v for k, v in details.items()},
        )

    def start_due(self, schedule_id: UUID, tenant_id: UUID) -> str:
        """Open the occurrence for the schedule's due time (idempotent per due time),
        advance the schedule, and start the run as the freshly reloaded owner."""

        with tenant_transaction(self.engine, tenant_id) as conn:
            schedule = store.load_schedule(conn, schedule_id, lock=True)
            if schedule is None or not schedule.active:
                return "inactive"
            due = conn.execute(
                text(
                    "SELECT next_run_at <= clock_timestamp() FROM briefing_schedules WHERE id = :id"
                ),
                {"id": schedule_id},
            ).scalar_one()
            if not due:
                return "not_due"
            epoch = int(
                conn.execute(
                    text("SELECT policy_epoch FROM tenants WHERE id = :t"), {"t": tenant_id}
                ).scalar_one()
            )
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO briefing_occurrences (id, tenant_id, schedule_id, due_at,
                                                      policy_epoch, status)
                    VALUES (:id, :t, :s, :due, :epoch, 'running')
                    ON CONFLICT (tenant_id, schedule_id, due_at) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "s": schedule_id,
                    "due": schedule.next_run_at,
                    "epoch": epoch,
                },
            ).scalar_one_or_none()
            # Skip missed slots: the next run is the first slot strictly in the future.
            conn.execute(
                text(
                    """
                    UPDATE briefing_schedules SET updated_at = clock_timestamp(),
                        next_run_at = next_run_at + make_interval(secs => interval_seconds) *
                            (floor(extract(epoch FROM clock_timestamp() - next_run_at)
                                   / interval_seconds) + 1)
                     WHERE id = :id
                    """
                ),
                {"id": schedule_id},
            )
            if inserted is None:
                return "occurrence_exists"
            occurrence_id: UUID = inserted
            blocked = self._owner_problem(conn, schedule.owner_user_id)
            if blocked is not None:
                store.update_occurrence(
                    conn, occurrence_id, status="blocked", status_reason=blocked
                )
                self._audit(
                    conn,
                    tenant_id,
                    schedule.owner_user_id,
                    "deny",
                    blocked,
                    schedule_id=schedule_id,
                    occurrence_id=occurrence_id,
                )
                return "blocked"
            self._audit(
                conn,
                tenant_id,
                schedule.owner_user_id,
                "allow",
                "occurrence_started",
                schedule_id=schedule_id,
                occurrence_id=occurrence_id,
                policy_epoch=epoch,
            )
        return self._ensure_run(occurrence_id, tenant_id)

    @staticmethod
    def _owner_problem(conn: Any, owner: UUID) -> str | None:
        subject = load_subject(conn, owner)
        if subject is None or not subject.active:
            return "owner_inactive"
        packs = load_settings(conn).enabled_packs
        if "actions" not in packs:
            return "pack_disabled"
        if "sales_reports" not in packs:
            return "report_pack_disabled"
        return None

    def _ensure_run(self, occurrence_id: UUID, tenant_id: UUID) -> str:
        with tenant_transaction(self.engine, tenant_id) as conn:
            occurrence = store.load_occurrence(conn, occurrence_id)
            if occurrence is None or occurrence.status != "running":
                return "not_running"
            if occurrence.run_id is not None:
                return "running"
            schedule = store.load_schedule(conn, occurrence.schedule_id)
            assert schedule is not None  # noqa: S101
            blocked = self._owner_problem(conn, schedule.owner_user_id)
            epoch = int(
                conn.execute(
                    text("SELECT policy_epoch FROM tenants WHERE id = :t"), {"t": tenant_id}
                ).scalar_one()
            )
            if blocked is not None:
                store.update_occurrence(
                    conn, occurrence_id, status="blocked", status_reason=blocked
                )
                return "blocked"
        # Deterministic client request id: a crash here re-creates the same run.
        run = self.create_run(
            tenant_id,
            schedule.owner_user_id,
            epoch,
            schedule.question,
            f"briefing-{occurrence_id.hex}",
            "report",
        )
        with tenant_transaction(self.engine, tenant_id) as conn:
            store.update_occurrence(conn, occurrence_id, run_id=run.id)
        return "running"

    def advance(self, occurrence_id: UUID, tenant_id: UUID) -> str:
        """When the run finished: a published result becomes a draft for review."""

        with tenant_transaction(self.engine, tenant_id) as conn:
            occurrence = store.load_occurrence(conn, occurrence_id, lock=True)
            if occurrence is None or occurrence.status != "running":
                return "not_running"
            if occurrence.run_id is None:
                pass  # created below, outside this transaction
            else:
                schedule = store.load_schedule(conn, occurrence.schedule_id)
                run = report_store.load_run(conn, occurrence.run_id)
                assert schedule is not None and run is not None  # noqa: S101
                if run.status not in TERMINAL_STATUSES:
                    return "running"
                if run.status is not RunStatus.PUBLISHED:
                    reason = f"run_{run.status.value}:{run.outcome_code or 'none'}"[:120]
                    store.update_occurrence(
                        conn, occurrence_id, status="failed", status_reason=reason
                    )
                    self._audit(
                        conn,
                        tenant_id,
                        schedule.owner_user_id,
                        "info",
                        "run_not_published",
                        occurrence_id=occurrence_id,
                        run_id=run.id,
                        outcome=run.outcome_code,
                    )
                    return "failed"
                owner = store.load_user(conn, schedule.owner_user_id)
                try:
                    with conn.begin_nested():
                        action = create_draft_in(
                            conn,
                            tenant_id=tenant_id,
                            owner_user_id=schedule.owner_user_id,
                            source_run_id=run.id,
                            recipient_user_id=schedule.recipient_user_id,
                            origin="briefing",
                            sender_name=owner.display_name if owner else "",
                            request_id=None,
                        )
                except Denied as denied:
                    store.update_occurrence(
                        conn, occurrence_id, status="blocked", status_reason=denied.reason
                    )
                    self._audit(
                        conn,
                        tenant_id,
                        schedule.owner_user_id,
                        "deny",
                        denied.reason,
                        occurrence_id=occurrence_id,
                        run_id=run.id,
                    )
                    return "blocked"
                store.update_occurrence(
                    conn,
                    occurrence_id,
                    status="review",
                    status_reason="awaiting_human_approval",
                    action_id=action.id,
                )
                self._audit(
                    conn,
                    tenant_id,
                    schedule.owner_user_id,
                    "info",
                    "draft_for_review",
                    occurrence_id=occurrence_id,
                    run_id=run.id,
                    action_id=action.id,
                )
                return "review"
        return self._ensure_run(occurrence_id, tenant_id)


@dataclass
class ActionWorker:
    """One pass over due action work. The Celery beat task calls ``tick``; tests and
    the demo call it directly. Deliveries go through ``dispatch`` (Celery) or run
    inline when ``dispatch`` is None."""

    engine: Engine
    settings: Settings
    delivery: EmailDelivery
    briefings: BriefingRunner | None
    dispatch: Callable[[UUID, UUID], None] | None = None

    def tick(self) -> dict[str, list[str]]:
        outcomes: dict[str, list[str]] = {}
        for work in due_work(self.engine, self.settings):
            try:
                outcome = self._one(work)
            except Exception:
                logger.exception("action_work_failed", extra={"kind": work.kind})
                outcome = "error"
            outcomes.setdefault(work.kind, []).append(outcome)
        return outcomes

    def _one(self, work: Any) -> str:
        if work.kind == "deliver":
            if self.dispatch is not None:
                self.dispatch(work.id, work.tenant_id)
                return "dispatched"
            return self.delivery.deliver(work.id, work.tenant_id)
        if work.kind == "stale":
            return self.delivery.expire_lease(work.id, work.tenant_id)
        if work.kind == "reconcile":
            return self.delivery.reconcile(work.id, work.tenant_id)
        if self.briefings is None:
            return "briefings_unavailable"
        if work.kind == "schedule":
            return self.briefings.start_due(work.id, work.tenant_id)
        if work.kind == "briefing":
            return self.briefings.advance(work.id, work.tenant_id)
        return "unknown"

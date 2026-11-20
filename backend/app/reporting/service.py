"""Report runs: creation with a durable job row, dispatch, execution, cancellation,
authorized event streaming/replay and authorized evidence access."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import Engine, text

from app.config import ModelMode, ReportDispatch, Settings
from app.db.engine import contextless_transaction, tenant_transaction
from app.reporting import store
from app.reporting.access import authorize_dataset
from app.reporting.contracts import (
    CONTENT_EVENTS,
    ERROR_MESSAGES,
    STEP_LABELS,
    TERMINAL_STATUSES,
    ErrorCode,
    EventType,
    RunStatus,
    Step,
)
from app.reporting.model_client import FAKE_LABEL
from app.reporting.workflow import ReportWorkflow
from app.repositories.audit import record_audit_event
from app.security.context import AccessContext

logger = logging.getLogger("eaios.reports")


class RunNotFound(Exception):
    """Unknown, foreign or no-longer-authorized run or evidence (uniform 404)."""


class AccessLost(Exception):
    """Authentication or authorization failed during a stream."""


class Dispatcher(Protocol):
    def dispatch(self, run_id: UUID, tenant_id: UUID) -> None: ...


@dataclass
class ReportRunner:
    """Executes one job. Safe under duplicate delivery: only one worker can claim a
    job at a time, and a run that already reached a terminal state is not re-run."""

    app_engine: Engine
    workflow: ReportWorkflow
    settings: Settings

    def run_job(self, run_id: UUID, tenant_id: UUID) -> str:
        with tenant_transaction(self.app_engine, tenant_id) as conn:
            run = store.load_run(conn, run_id)
            if run is None:
                return "missing"
            if run.status in TERMINAL_STATUSES:
                store.finish_job(conn, run_id)
                return "already_finished"
            attempt = store.claim_job(conn, run_id, self.settings.report_job_lease_seconds)
            if attempt is None:
                return "not_claimed"
        if attempt > self.settings.report_job_max_attempts:
            self._fail_exhausted(run_id, tenant_id, run.owner_user_id)
            return "attempts_exhausted"
        self.workflow.run(run_id, tenant_id, run.owner_user_id, run.question, run.inference_mode)
        return "ran"

    def _fail_exhausted(self, run_id: UUID, tenant_id: UUID, user_id: UUID) -> None:
        with tenant_transaction(self.app_engine, tenant_id, user_id) as conn:
            run = store.lock_run(conn, run_id)
            if run is None or run.status in TERMINAL_STATUSES:
                return
            store.append_event(
                conn,
                run_id,
                tenant_id,
                EventType.ERROR,
                {
                    "code": ErrorCode.INTERNAL.value,
                    "message": ERROR_MESSAGES[ErrorCode.INTERNAL],
                },
            )
            store.append_event(
                conn,
                run_id,
                tenant_id,
                EventType.DONE,
                {
                    "message_id": None,
                    "outcome": "failed",
                    "verification": None,
                    "abstention": None,
                    "inference": run.inference_mode,
                },
            )
            store.update_run(
                conn,
                run_id,
                status=RunStatus.FAILED,
                outcome_code="job_attempts_exhausted",
                finished_at="now",
            )
            store.finish_job(conn, run_id)


class InlineDispatcher:
    """Runs jobs on a small thread pool inside the API process (tests, single-process
    development). The job row is still the source of truth."""

    def __init__(self, runner: ReportRunner, workers: int = 2) -> None:
        self._runner = runner
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="report")

    def dispatch(self, run_id: UUID, tenant_id: UUID) -> None:
        self._pool.submit(self._safe, run_id, tenant_id)

    def _safe(self, run_id: UUID, tenant_id: UUID) -> None:
        try:
            self._runner.run_job(run_id, tenant_id)
        except Exception:
            logger.exception("inline_report_job_failed")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


class CeleryDispatcher:
    """Publishes a Celery message after the run/job transaction committed. If the
    publish fails, the beat task re-dispatches pending jobs from PostgreSQL."""

    def __init__(self, app_engine: Engine, send: Callable[[str, list[str]], None]) -> None:
        self._engine = app_engine
        self._send = send

    def dispatch(self, run_id: UUID, tenant_id: UUID) -> None:
        try:
            self._send("app.tasks.reports.run_report", [str(run_id), str(tenant_id)])
        except Exception:
            logger.warning("report_dispatch_failed", extra={"run_id": str(run_id)})
            return
        with tenant_transaction(self._engine, tenant_id) as conn:
            store.mark_dispatched(conn, run_id)


def dispatch_due_jobs(app_engine: Engine, dispatcher: Dispatcher, limit: int = 20) -> int:
    """Re-dispatch pending jobs not dispatched recently and lease-expired claims."""

    with contextless_transaction(app_engine) as conn:
        due = conn.execute(
            text("SELECT run_id, tenant_id FROM report_jobs_due(:n, interval '30 seconds')"),
            {"n": limit},
        ).all()
    for row in due:
        dispatcher.dispatch(row.run_id, row.tenant_id)
    return len(due)


def sse_frame(seq: int | None, event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {data}\n\n"


class ReportService:
    def __init__(
        self,
        app_engine: Engine,
        settings: Settings,
        dispatcher: Dispatcher | None,
        reauthenticate: Callable[[str], AccessContext],
    ) -> None:
        self._engine = app_engine
        self._settings = settings
        self._dispatcher = dispatcher
        self._reauthenticate = reauthenticate

    @property
    def available(self) -> bool:
        return self._dispatcher is not None

    @property
    def inference_mode(self) -> str:
        return self._settings.model_mode.value

    def create_run(self, ctx: AccessContext, question: str, client_request_id: str) -> store.RunRow:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run, created = store.create_run(
                conn,
                tenant_id=ctx.tenant_id,
                owner_user_id=ctx.user_id,
                question=question,
                client_request_id=client_request_id,
                inference_mode=self.inference_mode,
                policy_epoch=ctx.policy_epoch,
            )
            if created:
                payload: dict[str, Any] = {
                    "step": Step.ACCEPTED.value,
                    "state": "completed",
                    "label": STEP_LABELS[Step.ACCEPTED],
                    "run_id": str(run.id),
                    "inference": self.inference_mode,
                }
                if self.inference_mode == ModelMode.FAKE.value:
                    payload["inference_label"] = FAKE_LABEL
                store.append_event(conn, run.id, ctx.tenant_id, EventType.STATUS, payload)
                record_audit_event(
                    conn,
                    request_id=ctx.request_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    event_type="report.run",
                    action="report.create",
                    decision="info",
                    reason="run_created",
                    details={"run_id": str(run.id), "inference": self.inference_mode},
                )
        if created and self._dispatcher is not None:
            self._dispatcher.dispatch(run.id, ctx.tenant_id)
        return run

    def get_run(self, ctx: AccessContext, run_id: UUID) -> tuple[store.RunRow, bool]:
        """Return the owner's run and whether its published content may be shown now."""

        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            if run is None:
                raise RunNotFound
            content_allowed = True
            if run.status is RunStatus.PUBLISHED:
                content_allowed = authorize_dataset(conn, ctx.user_id, run.resource_id).allowed
        return run, content_allowed

    def cancel(self, ctx: AccessContext, run_id: UUID) -> store.RunRow:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            if run is None:
                raise RunNotFound
            if run.status not in TERMINAL_STATUSES:
                run = store.request_cancel(conn, run_id, ctx.user_id) or run
                record_audit_event(
                    conn,
                    request_id=ctx.request_id,
                    tenant_id=ctx.tenant_id,
                    user_id=ctx.user_id,
                    event_type="report.run",
                    action="report.cancel",
                    decision="info",
                    reason="cancel_requested",
                    details={"run_id": str(run_id)},
                )
        return run

    # -- streaming -----------------------------------------------------------------

    def _check_access(self, token: str, run_id: UUID, content: bool) -> AccessContext:
        """Fresh authentication and, for content, fresh dataset authorization."""

        try:
            ctx = self._reauthenticate(token)
        except Exception as exc:
            raise AccessLost("session") from exc
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            run = store.load_run(conn, run_id, ctx.user_id)
            if run is None:
                raise RunNotFound
            if content:
                decision = authorize_dataset(conn, ctx.user_id, run.resource_id)
                if not decision.allowed:
                    record_audit_event(
                        conn,
                        request_id=ctx.request_id,
                        tenant_id=ctx.tenant_id,
                        user_id=ctx.user_id,
                        event_type="report.replay",
                        action="dataset.query",
                        resource_id=run.resource_id,
                        decision="deny",
                        reason=decision.reason.value,
                        details={"run_id": str(run_id)},
                    )
                    raise AccessLost(decision.reason.value)
        return ctx

    def open_stream(self, token: str, run_id: UUID) -> AccessContext:
        """Authorize before any byte is streamed (raises RunNotFound for foreign runs)."""

        return self._check_access(token, run_id, content=False)

    def stream(self, token: str, ctx: AccessContext, run_id: UUID, after: int) -> Iterator[str]:
        """Yield SSE frames from ``after`` until the terminal ``done`` event.

        Only persisted events are sent, so the stream and a later replay are identical.
        Content events are re-authorized immediately before they are sent; on failure
        the stream sends one ``error`` (``access_revoked``) and stops.
        """

        s = self._settings
        cursor = after
        started = last_beat = last_auth = time.monotonic()
        yield "retry: 3000\n\n"
        while True:
            now = time.monotonic()
            if now - started > s.sse_max_stream_seconds:
                return
            try:
                if now - last_auth > 5.0:
                    self._check_access(token, run_id, content=False)
                    last_auth = now
                with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
                    events = store.events_after(conn, run_id, cursor)
                if any(
                    e.event_type in CONTENT_EVENTS
                    or (e.event_type is EventType.DONE and e.payload.get("outcome") == "published")
                    for e in events
                ):
                    self._check_access(token, run_id, content=True)
                    last_auth = time.monotonic()
            except (AccessLost, RunNotFound):
                yield sse_frame(
                    None,
                    EventType.ERROR.value,
                    {
                        "code": ErrorCode.ACCESS_REVOKED.value,
                        "message": ERROR_MESSAGES[ErrorCode.ACCESS_REVOKED],
                    },
                )
                return
            for event in events:
                cursor = event.seq
                yield sse_frame(
                    event.seq,
                    event.event_type.value,
                    {
                        "run_id": str(run_id),
                        "version": event.version,
                        **event.payload,
                    },
                )
                if event.event_type is EventType.DONE:
                    return
            if not events:
                if time.monotonic() - last_beat >= s.sse_heartbeat_seconds:
                    last_beat = time.monotonic()
                    yield ": heartbeat\n\n"
                time.sleep(s.sse_poll_interval_seconds)

    # -- evidence ------------------------------------------------------------------

    def evidence(self, ctx: AccessContext, evidence_id: UUID) -> dict[str, Any]:
        with tenant_transaction(self._engine, ctx.tenant_id, ctx.user_id) as conn:
            row = store.load_evidence(conn, evidence_id, ctx.user_id)
            run = store.load_run(conn, row.run_id, ctx.user_id) if row else None
            decision_reason = "not_found"
            allowed = False
            if row is not None and run is not None and run.status is RunStatus.PUBLISHED:
                decision = authorize_dataset(conn, ctx.user_id, row.resource_id)
                allowed, decision_reason = decision.allowed, decision.reason.value
            elif row is not None:
                decision_reason = "run_not_published"
            record_audit_event(
                conn,
                request_id=ctx.request_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                event_type="report.evidence",
                action="evidence.read",
                resource_id=row.resource_id if row and allowed else None,
                decision="allow" if allowed else "deny",
                reason=decision_reason,
                details={"evidence_id": str(evidence_id)},
            )
        if not allowed or row is None:
            raise RunNotFound
        recomputed = row.recomputed_sha256()
        return {
            "evidence_id": str(row.id),
            "run_id": str(row.run_id),
            "report_key": row.report_key,
            "executed_sql": row.executed_sql,
            "parameters": row.parameters,
            "columns": row.columns,
            "rows": row.rows,
            "row_count": row.row_count,
            "semantics": row.semantics,
            "dataset_fingerprint": row.dataset_fingerprint,
            "policy_epoch": row.policy_epoch,
            "executed_at": row.executed_at.isoformat(),
            "result_sha256": row.result_sha256,
            "integrity": "verified" if recomputed == row.result_sha256 else "mismatch",
        }


def build_dispatcher(
    settings: Settings, runner: ReportRunner | None, app_engine: Engine
) -> Dispatcher | None:
    if settings.report_dispatch is ReportDispatch.INLINE:
        return InlineDispatcher(runner) if runner is not None else None
    # Imported lazily: the Celery app reads REDIS_URL at import time.
    from app.tasks.worker import celery_app  # noqa: PLC0415

    def send(name: str, args: list[str]) -> None:
        celery_app.send_task(name, args=args, queue="default")

    return CeleryDispatcher(app_engine, send)

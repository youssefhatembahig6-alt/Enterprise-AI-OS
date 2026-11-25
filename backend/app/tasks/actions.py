"""Celery tasks for M6 actions.

``deliver_email`` may run more than once for one outbox row (acks_late, beat
re-dispatch); the ``pending -> dispatching`` claim under a row lock makes duplicates
no-ops. ``process_due_actions`` (beat, every 30 s) re-dispatches pending sends, turns
expired claims into ``uncertain``, reconciles uncertain sends against Mailpit, starts
due briefing occurrences and turns finished briefing runs into drafts for review.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from app.actions.briefings import ActionWorker, BriefingRunner
from app.actions.delivery import EmailDelivery
from app.actions.mail import MailpitLookup, SmtpMailer
from app.actions.store import mark_outbox_dispatched
from app.config import get_settings
from app.db.engine import create_app_engine
from app.logging_setup import configure_logging
from app.reporting.service import CeleryDispatcher, ReportService, build_dispatcher
from app.tasks.worker import celery_app

logger = logging.getLogger("eaios.tasks.actions")
_lock = threading.Lock()


@dataclass
class _Components:
    delivery: EmailDelivery
    worker: ActionWorker


_components: _Components | None = None


def _get() -> _Components:
    global _components  # noqa: PLW0603 - one set of pools per worker process
    with _lock:
        if _components is None:
            configure_logging()
            settings = get_settings()
            engine = create_app_engine(settings.database_url.get_secret_value(), pool_size=2)
            delivery = EmailDelivery(
                engine,
                settings,
                SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_timeout_seconds),
                MailpitLookup(settings.mailpit_api_url),
            )

            def send(name: str, args: list[str]) -> None:
                celery_app.send_task(name, args=args, queue="default")

            email_dispatch = CeleryDispatcher(
                engine, send, "app.tasks.actions.deliver_email", mark_outbox_dispatched
            )
            report_dispatch = build_dispatcher(settings, None, engine)
            reports = ReportService(engine, settings, report_dispatch, reauthenticate=_no_tokens)
            worker = ActionWorker(
                engine,
                settings,
                delivery,
                BriefingRunner(engine, reports.create_run_for),
                email_dispatch.dispatch,
            )
            _components = _Components(delivery, worker)
        return _components


def _no_tokens(_: str) -> NoReturn:
    raise RuntimeError("workers never authenticate bearer tokens")


@celery_app.task(  # type: ignore[misc]
    name="app.tasks.actions.deliver_email", acks_late=True, time_limit=120, soft_time_limit=90
)
def deliver_email(execution_key: str, tenant_id: str) -> str:
    outcome = _get().delivery.deliver(UUID(execution_key), UUID(tenant_id))
    logger.info("email_delivery", extra={"execution_key": execution_key, "outcome": outcome})
    return outcome


@celery_app.task(name="app.tasks.actions.process_due_actions")  # type: ignore[misc]
def process_due_actions() -> dict[str, list[str]]:
    outcomes = _get().worker.tick()
    if outcomes:
        logger.info("action_work", extra={"outcomes": outcomes})
    return outcomes

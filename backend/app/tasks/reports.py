"""Celery tasks for report runs (durable job row in PostgreSQL is the source of truth).

``run_report`` may be delivered more than once (acks_late, beat re-dispatch); the job
claim in ``ReportRunner.run_job`` makes duplicates no-ops, and the unique ``done``
index guarantees one terminal event per run.
"""

from __future__ import annotations

import logging
import os
import threading
from uuid import UUID

from app.config import get_settings
from app.db.engine import create_app_engine
from app.logging_setup import configure_logging
from app.reporting.factory import build_runner
from app.reporting.service import CeleryDispatcher, ReportRunner, dispatch_due_jobs
from app.tasks.worker import celery_app

logger = logging.getLogger("eaios.tasks.reports")
_lock = threading.Lock()
_runner: ReportRunner | None = None


def _get_runner() -> ReportRunner:
    global _runner  # noqa: PLW0603 - one runner (and pools) per worker process
    with _lock:
        if _runner is None:
            configure_logging()
            settings = get_settings()
            engine = create_app_engine(settings.database_url.get_secret_value(), pool_size=2)
            built = build_runner(settings, engine)
            if built is None:
                raise RuntimeError("REPORT_DATABASE_URL is not configured for the worker")
            _runner = built[0]
        return _runner


@celery_app.task(  # type: ignore[misc]
    name="app.tasks.reports.run_report",
    time_limit=1200,
    soft_time_limit=1100,
    acks_late=True,
)
def run_report(run_id: str, tenant_id: str) -> str:
    outcome = _get_runner().run_job(UUID(run_id), UUID(tenant_id))
    logger.info("report_job", extra={"run_id": run_id, "outcome": outcome})
    return outcome


@celery_app.task(name="app.tasks.reports.dispatch_due_report_jobs")  # type: ignore[misc]
def dispatch_due_report_jobs() -> int:
    configure_logging()
    engine = create_app_engine(os.environ["DATABASE_URL"], pool_size=1)
    try:

        def send(name: str, args: list[str]) -> None:
            celery_app.send_task(name, args=args, queue="default")

        return dispatch_due_jobs(engine, CeleryDispatcher(engine, send))
    finally:
        engine.dispose()

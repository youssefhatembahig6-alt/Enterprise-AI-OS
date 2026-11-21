"""Celery tasks for document ingestion. The ``ingestion_jobs`` row is the source of
truth: duplicate deliveries are no-ops (job claim with lease), and the beat task
re-dispatches pending jobs whose message was lost and claims whose lease expired."""

from __future__ import annotations

import logging
import os
import threading
from uuid import UUID

from sqlalchemy import text

from app.config import get_settings
from app.db.engine import contextless_transaction, create_app_engine
from app.documents import store as doc_store
from app.documents.ingest import IngestionRunner
from app.logging_setup import configure_logging
from app.reporting.factory import build_components
from app.reporting.service import CeleryDispatcher
from app.tasks.worker import celery_app

logger = logging.getLogger("eaios.tasks.documents")
_lock = threading.Lock()
_runner: IngestionRunner | None = None


def _get_runner() -> IngestionRunner:
    global _runner  # noqa: PLW0603 - one runner (and pools) per worker process
    with _lock:
        if _runner is None:
            configure_logging()
            settings = get_settings()
            engine = create_app_engine(settings.database_url.get_secret_value(), pool_size=2)
            _runner = build_components(settings, engine).ingestion
        return _runner


@celery_app.task(  # type: ignore[misc]
    name="app.tasks.documents.ingest_document", time_limit=900, soft_time_limit=840, acks_late=True
)
def ingest_document(job_id: str, tenant_id: str) -> str:
    outcome = _get_runner().run_job(UUID(job_id), UUID(tenant_id))
    logger.info("ingestion_job", extra={"job_id": job_id, "outcome": outcome})
    return outcome


@celery_app.task(name="app.tasks.documents.dispatch_due_ingestion_jobs")  # type: ignore[misc]
def dispatch_due_ingestion_jobs() -> int:
    configure_logging()
    engine = create_app_engine(os.environ["DATABASE_URL"], pool_size=1)
    try:

        def send(name: str, args: list[str]) -> None:
            celery_app.send_task(name, args=args, queue="default")

        dispatcher = CeleryDispatcher(
            engine, send, "app.tasks.documents.ingest_document", doc_store.mark_job_dispatched
        )
        with contextless_transaction(engine) as conn:
            due = conn.execute(
                text("SELECT job_id, tenant_id FROM ingestion_jobs_due(20, interval '30 seconds')")
            ).all()
        for row in due:
            dispatcher.dispatch(row.job_id, row.tenant_id)
        return len(due)
    finally:
        engine.dispose()

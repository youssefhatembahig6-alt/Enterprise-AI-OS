"""Celery application (worker and beat share this module).

M0 has one real maintenance task: purging long-expired session rows through the
restricted ``purge_expired_sessions()`` database function.
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from sqlalchemy import text

from app.db.engine import create_app_engine
from app.logging_setup import configure_logging

logger = logging.getLogger("eaios.tasks")


def _broker_url() -> str:
    return os.environ["REDIS_URL"]


celery_app = Celery("eaios", broker=_broker_url(), include=["app.tasks.worker"])
celery_app.conf.update(
    task_default_queue="default",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=60,
    task_soft_time_limit=45,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    worker_redirect_stdouts=False,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "purge-expired-sessions": {
            "task": "app.tasks.worker.purge_expired_sessions",
            "schedule": 3600.0,
        }
    },
)


@celery_app.task(name="app.tasks.worker.purge_expired_sessions")  # type: ignore[misc]
def purge_expired_sessions() -> int:
    configure_logging()
    engine = create_app_engine(os.environ["DATABASE_URL"], pool_size=1)
    try:
        with engine.begin() as conn:
            removed = int(conn.execute(text("SELECT purge_expired_sessions()")).scalar_one())
    finally:
        engine.dispose()
    logger.info("purged_expired_sessions", extra={"removed": removed})
    return removed

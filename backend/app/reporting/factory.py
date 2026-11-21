"""Wiring for the report runner (API inline mode and the Celery worker)."""

from __future__ import annotations

from sqlalchemy import Engine

from app.config import Settings
from app.reporting.executor import ReportExecutor, create_report_engine
from app.reporting.model_client import ModelClient, build_model_client
from app.reporting.service import ReportRunner
from app.reporting.workflow import ReportWorkflow, WorkflowDeps


def build_runner(
    settings: Settings, app_engine: Engine, model: ModelClient | None = None
) -> tuple[ReportRunner, Engine] | None:
    """Return the runner and its report-role engine, or None if reports are not
    configured (no ``REPORT_DATABASE_URL``)."""

    if settings.report_database_url is None:
        return None
    report_engine = create_report_engine(
        settings.report_database_url.get_secret_value(), pool_size=settings.report_pool_size
    )
    executor = ReportExecutor(
        app_engine,
        report_engine,
        statement_timeout_ms=settings.report_statement_timeout_ms,
        max_rows=settings.report_max_rows,
    )
    workflow = ReportWorkflow(
        WorkflowDeps(
            app_engine=app_engine,
            executor=executor,
            model=model or build_model_client(settings),
            settings=settings,
        )
    )
    return ReportRunner(app_engine, workflow, settings), report_engine

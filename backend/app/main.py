"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis

from app.api import auth, health, reports
from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.db.engine import create_app_engine
from app.logging_setup import configure_logging, request_id_var
from app.reporting.factory import build_runner
from app.reporting.model_client import ModelClient
from app.reporting.service import InlineDispatcher, ReportService, build_dispatcher
from app.security.passwords import PasswordVerifier
from app.security.ratelimit import LoginRateLimiter
from app.services.health import ReadinessChecker
from app.services.identity import IdentityService


def create_app(settings: Settings | None = None, model: ModelClient | None = None) -> FastAPI:
    """Build the API. ``model`` overrides the model client (tests inject doubles)."""

    settings = settings or get_settings()
    configure_logging()

    engine = create_app_engine(
        settings.database_url.get_secret_value(), pool_size=settings.database_pool_size
    )
    redis = Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.readiness_timeout_seconds,
        socket_connect_timeout=settings.readiness_timeout_seconds,
    )

    # Reports: the runner (and report-role engine) lives in the API only for inline
    # dispatch; with Celery the worker owns it and the API only publishes jobs.
    runner = None
    report_engine = None
    if settings.report_database_url is not None and settings.report_dispatch.value == "inline":
        built = build_runner(settings, engine, model)
        if built is not None:
            runner, report_engine = built
    dispatcher = (
        build_dispatcher(settings, runner, engine)
        if settings.report_database_url is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if isinstance(dispatcher, InlineDispatcher):
            dispatcher.shutdown()
        if report_engine is not None:
            report_engine.dispose()
        engine.dispose()
        redis.close()  # type: ignore[no-untyped-call]

    app = FastAPI(
        title="Enterprise AI OS API",
        version="0.2.0-m2",
        description=(
            "Identity, tenant isolation and health (M0); verified SQL reports streamed "
            "over Server-Sent Events (M2)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    limiter = LoginRateLimiter(
        redis,
        prefix=settings.rate_limit_key_prefix,
        max_per_identity=settings.login_max_failures_per_identity,
        max_per_client=settings.login_max_failures_per_client,
        window_seconds=settings.login_failure_window_seconds,
    )
    verifier = PasswordVerifier(
        concurrency=settings.password_verify_concurrency,
        queue_timeout_seconds=settings.password_verify_queue_timeout_seconds,
    )
    identity = IdentityService(engine, settings, limiter, verifier)
    app.state.identity = identity
    app.state.reports = ReportService(
        engine,
        settings,
        dispatcher,
        reauthenticate=lambda token: identity.authenticate(token, request_id_var.get()),
    )
    app.state.report_runner = runner
    app.state.readiness = ReadinessChecker(
        engine=engine,
        redis=redis,
        qdrant_url=settings.qdrant_url,
        s3_endpoint_url=settings.s3_endpoint_url,
        timeout=settings.readiness_timeout_seconds,
    )

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Always server-generated; an inbound X-Request-ID is not trusted.
        request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID", "Retry-After", "X-Run-ID"],
        max_age=600,
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(reports.router)
    return app

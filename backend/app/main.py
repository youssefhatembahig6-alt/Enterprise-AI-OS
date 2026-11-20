"""FastAPI application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis

from app.api import auth, health
from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.db.engine import create_app_engine
from app.logging_setup import configure_logging, request_id_var
from app.security.ratelimit import LoginRateLimiter
from app.services.health import ReadinessChecker
from app.services.identity import IdentityService


def create_app(settings: Settings | None = None) -> FastAPI:
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()
        redis.close()  # type: ignore[no-untyped-call]

    app = FastAPI(
        title="Enterprise AI OS API",
        version="0.1.0-m0",
        description="M0 foundation: identity, tenant isolation and health endpoints.",
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
    app.state.identity = IdentityService(engine, settings, limiter)
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
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    return app

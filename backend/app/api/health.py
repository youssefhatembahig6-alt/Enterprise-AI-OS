"""Liveness (/health) and readiness (/ready)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.schemas.auth import LivenessResponse, ReadinessResponse
from app.services.health import ReadinessChecker

router = APIRouter(tags=["health"])


@router.get("/health", response_model=LivenessResponse)
def liveness() -> LivenessResponse:
    """The process is up and can serve HTTP. Does not touch dependencies."""

    return LivenessResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def readiness(request: Request) -> JSONResponse:
    """Required dependencies are reachable and the schema is at the expected head.

    Model inference is external and not part of M0, so it is reported as not
    configured or unchecked and never as an available capability.
    """

    checker: ReadinessChecker = request.app.state.readiness
    report = checker.check()
    body = ReadinessResponse(
        status="ready" if report.ready else "not_ready",
        checks=report.checks,
        model_inference=(
            "external_unchecked"
            if request.app.state.settings.model_gateway_url
            else "not_configured"
        ),
    )
    return JSONResponse(status_code=200 if report.ready else 503, content=body.model_dump())

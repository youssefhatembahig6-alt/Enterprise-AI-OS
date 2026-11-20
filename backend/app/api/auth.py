"""Authentication and identity routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import CurrentContext, get_identity_service
from app.api.errors import ApiError
from app.logging_setup import request_id_var
from app.schemas.auth import (
    AttributesOut,
    CapabilityOut,
    ErrorResponse,
    IdentityResponse,
    LoginRequest,
    TenantOut,
    TokenResponse,
)
from app.security.ratelimit import RateLimiterUnavailable
from app.services.identity import IdentityService, LoginFailed, LoginRateLimited

router = APIRouter(prefix="/api/v1", tags=["identity"])

_AUTH_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    responses={**_AUTH_ERRORS, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
def login(
    body: LoginRequest,
    request: Request,
    service: Annotated[IdentityService, Depends(get_identity_service)],
) -> TokenResponse:
    client = request.client.host if request.client else "unknown"
    try:
        result = service.login(
            tenant_slug=body.tenant,
            email=body.email,
            password=body.password,
            client=client,
            request_id=request_id_var.get(),
        )
    except LoginRateLimited as exc:
        raise ApiError(
            429,
            "rate_limited",
            "Too many failed sign-in attempts. Try again later.",
            {"Retry-After": str(exc.retry_after)},
        ) from exc
    except LoginFailed as exc:
        raise ApiError(401, "invalid_credentials", "Invalid sign-in details.") from exc
    except RateLimiterUnavailable as exc:
        raise ApiError(503, "service_unavailable", "Sign-in is temporarily unavailable.") from exc
    return TokenResponse(access_token=result.access_token, expires_at=result.expires_at)


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"model": ErrorResponse}},
)
def logout(
    ctx: CurrentContext,
    service: Annotated[IdentityService, Depends(get_identity_service)],
) -> Response:
    service.logout(ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=IdentityResponse, responses={401: {"model": ErrorResponse}})
def me(ctx: CurrentContext) -> IdentityResponse:
    subject = ctx.subject
    return IdentityResponse(
        user_id=ctx.user_id,
        email=ctx.email,
        display_name=ctx.display_name,
        tenant=TenantOut(id=ctx.tenant_id, slug=ctx.tenant_slug, name=ctx.tenant_name),
        roles=list(ctx.role_keys),
        capabilities=sorted(
            (
                CapabilityOut(capability=c.capability, scope_department=c.scope_department)
                for c in subject.capabilities
            ),
            key=lambda c: (c.capability, c.scope_department or ""),
        ),
        groups=list(ctx.group_keys),
        attributes=AttributesOut(
            clearance=subject.clearance, country=subject.country, department=subject.department
        ),
        policy_epoch=ctx.policy_epoch,
        session_expires_at=ctx.session_expires_at,
    )
